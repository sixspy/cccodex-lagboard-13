"""双路抢推失败测：调度与对账同推同一源水位须占指针锁。

后交者不得推进水位，只记「水位绑定争用」；缺争用留痕即红。
"""
import sqlite3
import threading
import time

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


def _body(hour):
    return {"source": "订单源",
            "source_clock": f"2026-11-19T{hour:02d}:00:00",
            "ingest_clock": f"2026-11-19T{hour:02d}:40:00"}


def test_concurrent_promote_records_contention(monkeypatch):
    """两路同时推进：只能有一路成功，后交必须留争用痕。"""
    c = TestClient(app)
    h = _auth(c)

    in_lock = threading.Event()
    real_classify = appmod._classify
    held = {"once": False}

    def classify_with_hold(src, body):
        if not held["once"]:
            held["once"] = True
            in_lock.set()          # 已持指针锁
            time.sleep(0.6)        # 占锁，给后交撞上
        return real_classify(src, body)

    monkeypatch.setattr(appmod, "_classify", classify_with_hold)

    results = []

    def first_worker():
        r = c.post("/api/arrivals", headers=h, json=_body(10))
        results.append(("first", r.status_code, r.json()))

    t1 = threading.Thread(target=first_worker)
    t1.start()
    assert in_lock.wait(5), "第一路未进入指针锁"
    r2 = c.post("/api/arrivals", headers=h, json=_body(11))  # 后交
    t1.join()

    assert r2.status_code == 409, f"后交应被指针锁挡下：{r2.status_code} {r2.text}"
    assert results and results[0][1] == 200, f"先交应成功：{results}"

    ev = c.get("/api/binding-events", headers=h).json()["items"]
    assert any(e["kind"] == "争用" and e["source"] == "订单源" for e in ev), \
        f"缺水位绑定争用留痕：{ev}"

    wm = c.get("/api/watermarks", headers=h).json()["items"]
    row = [w for w in wm if w["source"] == "订单源"][0]
    with sqlite3.connect(appmod.DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(*) FROM arrivals WHERE source='订单源'").fetchone()[0]
    assert row["seq"] == count, f"水位与到达条数不符：seq={row['seq']} 到达={count}"

    v = c.get("/api/watermarks/verify", headers=h).json()
    assert v["ok"] is True, v["gaps"]
