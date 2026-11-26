"""清算模拟端自己的 SQLite 存储：与主服务数据库物理隔离。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_write_lock = threading.Lock()

SCHEMA = """
-- 清算端付款流水：ref 为业务方提供的幂等键，全表唯一
CREATE TABLE IF NOT EXISTS clearing_payments (
    id           INTEGER PRIMARY KEY,
    ref          TEXT NOT NULL UNIQUE,
    payee        TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    status       TEXT NOT NULL CHECK (status IN ('accepted', 'paid', 'failed')),
    detail       TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 演示/测试控制位：宕机开关、扣款后断连开关、下一笔强制失败开关
CREATE TABLE IF NOT EXISTS sim_controls (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    offline            INTEGER NOT NULL DEFAULT 0,
    drop_after_debit   INTEGER NOT NULL DEFAULT 0,
    fail_next          INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO sim_controls(id, offline, drop_after_debit, fail_next) "
            "VALUES (1, 0, 0, 0)"
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def tx(db_path: str):
    with _write_lock:
        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@contextmanager
def read(db_path: str):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
