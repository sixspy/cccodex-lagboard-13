import hashlib, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
CARD_PATH = ROOT / "docs" / "水位绑定卡.json"
SEAL_PATH = ROOT / "docs" / "水位绑定卡.seal"
SESSIONS: dict[str, str] = {}

# 绑定口径：甲径（水位与到达同一次提交，任一步失败两侧一并撤销）
BINDING_PATH = "甲"
# 指针锁等待窗口；等不到即判水位绑定争用
POINTER_LOCK_WAIT = 0.15

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def connect_pointer():
    """指针锁连接：短等待，抢不到立刻让位并留争用痕。"""
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=POINTER_LOCK_WAIT, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  sla_minutes INTEGER NOT NULL,
  output_mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS arrivals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  source_clock TEXT NOT NULL,
  ingest_clock TEXT NOT NULL,
  lag_minutes INTEGER NOT NULL,
  level TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catchups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  restored_mode TEXT NOT NULL,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL,
  after_arrival_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS watermarks(
  source TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  source_clock TEXT NOT NULL,
  arrival_id INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS binding_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  source TEXT NOT NULL,
  detail TEXT NOT NULL,
  arrival_id INTEGER,
  created_at TEXT NOT NULL
);
"""

def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript(SCHEMA)
        if "after_arrival_id" not in _columns(conn, "catchups"):
            conn.execute("ALTER TABLE catchups ADD COLUMN after_arrival_id INTEGER NOT NULL DEFAULT 0")
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源","2026-11-16T08:00:00","2026-11-16T08:35:00",35,"告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源","2026-11-16T07:00:00","2026-11-16T09:10:00",130,"违约"))
        _seed_watermarks(conn)

def _seed_watermarks(conn):
    """源目录与到达列表先对齐水位，五处对齐从不留空窗起步。"""
    for src in conn.execute("SELECT name FROM sources").fetchall():
        name = src["name"]
        if conn.execute("SELECT 1 FROM watermarks WHERE source=?", (name,)).fetchone():
            continue
        last = conn.execute(
            "SELECT id,source_clock FROM arrivals WHERE source=? ORDER BY id DESC LIMIT 1", (name,)
        ).fetchone()
        if last:
            seq = conn.execute("SELECT COUNT(*) c FROM arrivals WHERE source=?", (name,)).fetchone()["c"]
            conn.execute(
                "INSERT INTO watermarks(source,seq,source_clock,arrival_id,updated_at) VALUES (?,?,?,?,?)",
                (name, seq, last["source_clock"], last["id"], now_iso()),
            )
            conn.execute(
                "INSERT INTO binding_events(kind,source,detail,arrival_id,created_at) VALUES ('晋升',?,?,?,?)",
                (name, f"seq={seq} 水位与到达同进同退（初始对齐）", last["id"], now_iso()),
            )
        else:
            conn.execute(
                "INSERT INTO watermarks(source,seq,source_clock,arrival_id,updated_at) VALUES (?,?,?,?,?)",
                (name, 0, "", 0, now_iso()),
            )

def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"

def mode_of(level):
    return {"正常":"可写", "告警":"降级只读", "违约":"冻结"}[level]

# ---------- 水位绑定卡 ----------

def load_card() -> dict:
    """读卡并验封：卡缺失或封签不符一律判偏离。"""
    try:
        raw = CARD_PATH.read_bytes()
        seal = SEAL_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return {"ok": False, "card": None, "reason": "水位绑定卡缺失"}
    digest = hashlib.sha256(raw).hexdigest()
    if digest != seal:
        return {"ok": False, "card": None, "reason": "封签不符"}
    import json
    try:
        card = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"ok": False, "card": None, "reason": "卡面不可解析"}
    if card.get("path") != BINDING_PATH:
        return {"ok": False, "card": card, "reason": "绑定口径偏离"}
    if card.get("sealed") is not True:
        return {"ok": False, "card": card, "reason": "卡未封档"}
    return {"ok": True, "card": card, "reason": ""}

def record_event(kind: str, source: str, detail: str, arrival_id: int | None = None):
    """对账账册只追加；争用/回滚/偏离/拒写都要留痕。"""
    try:
        with sqlite3.connect(DB_PATH, timeout=5.0) as c:
            c.execute(
                "INSERT INTO binding_events(kind,source,detail,arrival_id,created_at) VALUES (?,?,?,?,?)",
                (kind, source, detail, arrival_id, now_iso()),
            )
    except sqlite3.Error:
        pass

def assert_card(source: str):
    state = load_card()
    if not state["ok"]:
        record_event("偏离", source, "指针偏了：" + state["reason"])
        raise HTTPException(409, "水位绑定卡偏离（" + state["reason"] + "），拒写")

# ---------- 到达与水位 ----------

class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str

def _classify(src, body):
    def parse(t):
        try:
            return datetime.fromisoformat(t)
        except ValueError as exc:
            raise HTTPException(400, f"时钟格式不可解析：{t}") from exc
    lag = int((parse(body.ingest_clock) - parse(body.source_clock)).total_seconds() // 60)
    if lag < 0: lag = 0
    lv = level_of(lag, src["sla_minutes"])
    return lag, lv, mode_of(lv)

def _write_arrival(conn, body, lag, level) -> int:
    """单插入口；绑定失败测在此注入异常。"""
    cur = conn.execute(
        "INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
        (body.source, body.source_clock, body.ingest_clock, lag, level))
    return cur.lastrowid

def _advance_watermark(conn, source, seq, source_clock, arrival_id):
    """水位推进；与到达同库同事务，失败由外层一并撤销。"""
    conn.execute(
        "INSERT INTO watermarks(source,seq,source_clock,arrival_id,updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET seq=excluded.seq, source_clock=excluded.source_clock, "
        "arrival_id=excluded.arrival_id, updated_at=excluded.updated_at",
        (source, seq, source_clock, arrival_id, now_iso()))

def _bind_arrival(body: ArrivalIn) -> dict:
    """甲径：推进水位与挂到达同库一次提交，任一步失败则两侧一并撤销。

    调度与对账同推同一源水位须先占指针锁（BEGIN IMMEDIATE）；
    后交只记「水位绑定争用」，不得推进。
    """
    conn = connect_pointer()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        conn.close()
        if "locked" in str(exc) or "busy" in str(exc):
            record_event("争用", body.source, "水位绑定争用：后交未占指针锁，让位不推进")
            raise HTTPException(409, "水位绑定争用：另一路正持指针锁") from exc
        raise

    try:
        src = conn.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src:
            raise HTTPException(404, "未知源", headers=None)
        lag, lv, mode = _classify(src, body)
        row = conn.execute("SELECT seq FROM watermarks WHERE source=?", (body.source,)).fetchone()
        seq = (row["seq"] if row else 0) + 1

        aid = _write_arrival(conn, body, lag, lv)          # 挂到达
        _advance_watermark(conn, body.source, seq, body.source_clock, aid)  # 推水位（同一事务）
        conn.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, body.source))
        conn.execute(
            "INSERT INTO binding_events(kind,source,detail,arrival_id,created_at) VALUES ('晋升',?,?,?,?)",
            (body.source, f"seq={seq} 水位与到达同进同退", aid, now_iso()))
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")   # 任一步失败：水位与到达两侧一并撤销
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    return {"lag_minutes": lag, "level": lv, "output_mode": mode, "watermark_seq": seq, "arrival_id": aid}

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}

@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    assert_card(body.source)
    return _bind_arrival(body)

@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}

@app.get("/api/watermarks")
def watermarks(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM watermarks ORDER BY source").fetchall()]}

class PromoteIn(BaseModel):
    source: str
    seq: int
    source_clock: str = ""

@app.post("/api/watermarks/promote")
def promote_watermark(body: PromoteIn, authorization: str | None = Header(default=None)):
    """只推水位数字、不挂到达的入口一律拒写。"""
    require_user(authorization)
    assert_card(body.source)
    record_event("拒写", body.source, "只改水位数字未经到达挂载，指针关卡判失败")
    raise HTTPException(400, "只改水位数字不许过闸：须与到达同进同退")

class SwitchIn(BaseModel):
    path: str

@app.post("/api/binding-card/switch")
def switch_card(body: SwitchIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    record_event("拒写", "*", f"尝试当日更换绑定专册至{body.path}")
    raise HTTPException(409, "指针当天不得更换绑定专册")

@app.get("/api/binding-card")
def binding_card(authorization: str | None = Header(default=None)):
    require_user(authorization)
    state = load_card()
    return {"ok": state["ok"], "reason": state["reason"], "card": state["card"],
            "implementation_path": BINDING_PATH}

@app.get("/api/watermarks/verify")
def verify_watermarks(authorization: str | None = Header(default=None)):
    require_user(authorization)
    return verify_surfaces()

def verify_surfaces() -> dict:
    """五处对齐：水位卡片、到达列表、源目录、补采附页、对账账册。"""
    gaps = []
    with connect() as c:
        src_rows = c.execute("SELECT * FROM sources ORDER BY id").fetchall()
        wm_rows = {r["source"]: dict(r) for r in c.execute("SELECT * FROM watermarks").fetchall()}
        arr_counts = {r["source"]: r["c"] for r in c.execute(
            "SELECT source, COUNT(*) c FROM arrivals GROUP BY source").fetchall()}
        last_arr = {r["source"]: dict(r) for r in c.execute(
            "SELECT a.* FROM arrivals a JOIN (SELECT source, MAX(id) mid FROM arrivals GROUP BY source) m "
            "ON a.source=m.source AND a.id=m.mid").fetchall()}
        last_catch = {r["source"]: dict(r) for r in c.execute(
            "SELECT k.* FROM catchups k JOIN (SELECT source, MAX(id) mid FROM catchups GROUP BY source) m "
            "ON k.source=m.source AND k.id=m.mid").fetchall()}
        last_promote = {r["source"]: dict(r) for r in c.execute(
            "SELECT e.* FROM binding_events e JOIN (SELECT source, MAX(id) mid FROM binding_events "
            "WHERE kind='晋升' GROUP BY source) m ON e.source=m.source AND e.id=m.mid").fetchall()}
        events = c.execute("SELECT * FROM binding_events ORDER BY id").fetchall()
        arrival_ids = {r["id"] for r in c.execute("SELECT id FROM arrivals").fetchall()}

    for s in src_rows:
        name = s["name"]
        wm = wm_rows.get(name)
        count = arr_counts.get(name, 0)
        last = last_arr.get(name)
        # ① 水位卡片：每源必有水位行；无到达时 seq=0 且 arrival_id=0
        if not wm:
            gaps.append(f"水位卡片缺行：{name}")
            continue
        if last is None:
            if wm["seq"] != 0 or wm["arrival_id"] != 0:
                gaps.append(f"水位卡片空走：{name} seq={wm['seq']} 但到达列表为空")
        else:
            # ② 到达列表：水位到位且指向最新一条到达
            if wm["arrival_id"] != last["id"]:
                gaps.append(f"水位未挂最新到达：{name} 水位={wm['arrival_id']} 最新到达={last['id']}")
            if wm["source_clock"] != last["source_clock"]:
                gaps.append(f"水位源时钟与到达不一致：{name}")
            if wm["seq"] != count:
                gaps.append(f"水位序号与到达条数不符：{name} seq={wm['seq']} 条数={count}")
            # ③ 源目录：输出态与最新分级一致，或其后已有补采恢复
            catch = last_catch.get(name)
            restored = catch is not None and catch["after_arrival_id"] >= last["id"]
            expected = mode_of(last["level"])
            if not restored and s["output_mode"] != expected:
                gaps.append(f"源目录输出态未对齐：{name} 为{s['output_mode']} 应为{expected}")
            # ④ 补采附页：补齐须有到达锚点，不得凭空恢复
            if catch and catch["after_arrival_id"] > (wm["arrival_id"] or 0):
                gaps.append(f"补采附页越界：{name} 补采锚点{catch['after_arrival_id']} 超水位{wm['arrival_id']}")
        # ⑤ 对账账册：每个源的晋升留痕须与水位同指一条到达
        prom = last_promote.get(name)
        if last is not None and wm["seq"] > 0 and (not prom or prom["arrival_id"] != wm["arrival_id"]):
            gaps.append(f"对账账册缺晋升留痕或与水位不同指：{name}")

    for e in events:
        if e["kind"] == "晋升" and e["arrival_id"] is not None:
            if e["arrival_id"] not in arrival_ids:
                gaps.append(f"对账账册留痕悬空：事件#{e['id']} 指向到达#{e['arrival_id']}")

    return {"ok": not gaps, "gaps": gaps,
            "surfaces": ["水位卡片", "到达列表", "源目录", "补采附页", "对账账册"],
            "checked_at": now_iso()}

@app.get("/api/binding-events")
def binding_events(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM binding_events ORDER BY id DESC").fetchall()]}

@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    assert_card(source)
    now = now_iso()
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        wm = c.execute("SELECT arrival_id FROM watermarks WHERE source=?", (source,)).fetchone()
        anchor = wm["arrival_id"] if wm else 0
        c.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
        c.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at,after_arrival_id) VALUES (?,?,?,?,?,?)",
                  (source, "成功", "可写", "补齐后恢复可写", now, anchor))
        return {"ok": True, "output_mode": "可写"}

@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
