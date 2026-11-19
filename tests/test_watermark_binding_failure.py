"""水位绑定失败测：到达挂载失败时，水位与到达必须两侧一并撤销。

钉死甲径：推进水位与挂到达同库一次提交；任一步失败则两侧一并撤销。
未通过 = 空窗（水位已走/到达残留），判红。
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as appmod
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.DATA", tmp_path)
    init_db()
    yield


def _auth(c):
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _snapshot():
    with sqlite3.connect(appmod.DB_PATH) as c:
        wm = c.execute("SELECT source, seq, arrival_id FROM watermarks ORDER BY source").fetchall()
        ar = c.execute("SELECT id, source FROM arrivals ORDER BY id").fetchall()
    return wm, ar


def _body(source="订单源"):
    return {"source": source, "source_clock": "2026-11-19T10:00:00", "ingest_clock": "2026-11-19T11:00:00"}


def test_arrival_failure_rolls_back_watermark(monkeypatch):
    """到达写入抛错：水位不得先走。空窗即红。"""
    c = TestClient(app)
    h = _auth(c)
    before_wm, before_ar = _snapshot()

    def boom(conn, body, lag, level):
        raise sqlite3.OperationalError("到达挂载失败：入库写入异常")

    monkeypatch.setattr(appmod, "_write_arrival", boom)
    with pytest.raises(sqlite3.OperationalError):
        c.post("/api/arrivals", headers=h, json=_body())

    after_wm, after_ar = _snapshot()
    assert after_wm == before_wm, f"水位被推进后未撤销（空窗）：{before_wm} -> {after_wm}"
    assert after_ar == before_ar, "到达列表出现残留"


def test_watermark_failure_rolls_back_arrival(monkeypatch):
    """水位侧失败：到达不得残留。回滚后仍有到达即红。"""
    c = TestClient(app)
    h = _auth(c)
    before_wm, before_ar = _snapshot()

    def boom(conn, source, seq, source_clock, arrival_id):
        raise sqlite3.OperationalError("水位推进失败")

    monkeypatch.setattr(appmod, "_advance_watermark", boom)
    with pytest.raises(sqlite3.OperationalError):
        c.post("/api/arrivals", headers=h, json=_body())

    after_wm, after_ar = _snapshot()
    assert after_ar == before_ar, f"回滚后仍残留到达（空窗）：{before_ar} -> {after_ar}"
    assert after_wm == before_wm, "水位被单侧推进"


def test_digits_only_rejected():
    """只改水位数字样式不许过闸。"""
    c = TestClient(app)
    h = _auth(c)
    before_wm, _ = _snapshot()
    r = c.post("/api/watermarks/promote", headers=h,
               json={"source": "订单源", "seq": 99, "source_clock": "2026-11-19T23:59:00"})
    assert r.status_code == 400
    after_wm, _ = _snapshot()
    assert after_wm == before_wm
    ev = c.get("/api/binding-events", headers=h).json()["items"]
    assert any(e["kind"] == "拒写" for e in ev)


def test_verify_has_no_gap_after_success():
    """正常晋升后五处对齐无空窗。"""
    c = TestClient(app)
    h = _auth(c)
    r = c.post("/api/arrivals", headers=h, json=_body())
    assert r.status_code == 200
    v = c.get("/api/watermarks/verify", headers=h).json()
    assert v["ok"] is True, v["gaps"]


def test_tampered_card_rejects_write_and_records_deviation(tmp_path, monkeypatch):
    """卡被改过（封签不符）即拒写，并记下指针偏了。"""
    card = tmp_path / "card.json"
    seal = tmp_path / "card.seal"
    import hashlib
    card.write_text('{"path":"乙","sealed":true}', encoding="utf-8")
    seal.write_text(hashlib.sha256(card.read_bytes()).hexdigest() + "\n", encoding="utf-8")
    monkeypatch.setattr(appmod, "CARD_PATH", card)
    monkeypatch.setattr(appmod, "SEAL_PATH", seal)

    c = TestClient(app)
    h = _auth(c)
    before_wm, before_ar = _snapshot()

    card.write_text('{"path":"甲","sealed":true}', encoding="utf-8")  # 改卡不重封
    r = c.post("/api/arrivals", headers=h, json=_body())
    assert r.status_code == 409
    assert _snapshot() == (before_wm, before_ar), "拒写后不得推进任何一侧"

    ev = c.get("/api/binding-events", headers=h).json()["items"]
    assert any(e["kind"] == "偏离" and "指针偏了" in e["detail"] for e in ev), ev


def test_switch_book_rejected():
    """指针当天不得更换绑定专册。"""
    c = TestClient(app)
    h = _auth(c)
    r = c.post("/api/binding-card/switch", headers=h, json={"path": "乙"})
    assert r.status_code == 409
    assert c.get("/api/binding-card", headers=h).json()["implementation_path"] == "甲"
