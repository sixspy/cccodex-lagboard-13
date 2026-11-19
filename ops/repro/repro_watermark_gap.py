#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开局前复现：水位指针先走、到达未挂；回滚后残留到达。

本脚本用两段提交的朴素写法（水位与到达各自 commit）在临时库上复现两种空窗，
不接触 data/app.db，也不代表正式写路径。
"""
import os
import sqlite3
import tempfile
from pathlib import Path

SCHEMA = """
CREATE TABLE sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  sla_minutes INTEGER NOT NULL,
  output_mode TEXT NOT NULL
);
CREATE TABLE arrivals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  source_clock TEXT NOT NULL,
  ingest_clock TEXT NOT NULL,
  lag_minutes INTEGER NOT NULL,
  level TEXT NOT NULL
);
CREATE TABLE watermarks(
  source TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  source_clock TEXT NOT NULL,
  arrival_id INTEGER NOT NULL
);
INSERT INTO sources(name,sla_minutes,output_mode) VALUES('订单源',15,'可写');
"""


def fresh_db() -> str:
    fd, path = tempfile.mkstemp(prefix="lagboard-repro-", suffix=".db")
    os.close(fd)
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
    return path


def state(path: str) -> str:
    with sqlite3.connect(path) as c:
        wm = c.execute("SELECT source,seq,arrival_id FROM watermarks").fetchall()
        ar = c.execute("SELECT id,source,lag_minutes,level FROM arrivals").fetchall()
    return f"watermarks={wm} arrivals={ar}"


def naive_watermark_first(path: str, fail_at: str) -> None:
    """水位先独立提交，随后挂到达；到达侧失败时水位已经走掉。"""
    with sqlite3.connect(path) as c:  # 第一次提交：水位先走
        c.execute(
            "INSERT OR REPLACE INTO watermarks(source,seq,source_clock,arrival_id) "
            "VALUES('订单源',1,'2026-11-19T10:00:00',1)"
        )
    if fail_at == "arrival":
        raise RuntimeError("到达挂载失败：入库写入异常（水位已提交）")
    with sqlite3.connect(path) as c:  # 第二次提交：到达后挂
        c.execute(
            "INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) "
            "VALUES('订单源','2026-11-19T10:00:00','2026-11-19T10:40:00',40,'违约')"
        )


def naive_arrival_then_watermark_rollback(path: str) -> None:
    """到达先独立提交，水位侧失败回滚，到达残留。"""
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) "
            "VALUES('订单源','2026-11-19T10:00:00','2026-11-19T10:40:00',40,'违约')"
        )
    c = sqlite3.connect(path)
    c.execute("BEGIN")
    c.execute(
        "INSERT OR REPLACE INTO watermarks(source,seq,source_clock,arrival_id) "
        "VALUES('订单源',1,'2026-11-19T10:00:00',1)"
    )
    c.rollback()  # 水位侧回滚，但到达已提交
    c.close()


def main() -> int:
    p1 = fresh_db()
    try:
        naive_watermark_first(p1, fail_at="arrival")
    except RuntimeError as exc:
        print(f"[场景A] {exc}")
    print(f"[场景A] 结果：{state(p1)}")
    print("[场景A] 判定：水位 seq=1 已走，到达 0 条 —— 水位已走到达未挂（空窗）")

    p2 = fresh_db()
    naive_arrival_then_watermark_rollback(p2)
    print(f"[场景B] 结果：{state(p2)}")
    print("[场景B] 判定：水位回滚为 0 条，到达残留 1 条 —— 回滚后仍残留到达（空窗）")

    for p in (p1, p2):
        Path(p).unlink(missing_ok=True)
    print("复现完成：两段提交写法必然产生空窗。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
