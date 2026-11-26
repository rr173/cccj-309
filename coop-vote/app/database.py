"""SQLite 连接、建表脚本与事务工具。

写事务在进程内用一把全局锁串行化，并配合 ``BEGIN IMMEDIATE``，
使关闭、改投、停权、重领等并发写操作具有确定的服务端接受顺序。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ballots (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('draft', 'open', 'settled')),
    created_at  TEXT NOT NULL,
    opened_at   TEXT,
    settled_at  TEXT
);

-- 开启表决时冻结的会员资格范围；suspend/resume 只改这张表
CREATE TABLE IF NOT EXISTS eligibilities (
    id         INTEGER PRIMARY KEY,
    ballot_id  INTEGER NOT NULL REFERENCES ballots(id),
    member_id  INTEGER NOT NULL REFERENCES members(id),
    status     TEXT NOT NULL CHECK (status IN ('active', 'suspended')),
    epoch      INTEGER NOT NULL DEFAULT 1,
    frozen_at  TEXT NOT NULL,
    UNIQUE (ballot_id, member_id)
);

-- 每个资格在冻结时获得一个随机匿名代号，投票只与代号绑定
CREATE TABLE IF NOT EXISTS pseudonyms (
    id             INTEGER PRIMARY KEY,
    ballot_id      INTEGER NOT NULL REFERENCES ballots(id),
    eligibility_id INTEGER NOT NULL UNIQUE REFERENCES eligibilities(id),
    voter_pid      TEXT NOT NULL UNIQUE,
    created_at     TEXT NOT NULL
);

-- 投票凭证：只存哈希；重领即旧凭证作废，资格/代号不新增
CREATE TABLE IF NOT EXISTS credentials (
    id             INTEGER PRIMARY KEY,
    ballot_id      INTEGER NOT NULL REFERENCES ballots(id),
    eligibility_id INTEGER NOT NULL REFERENCES eligibilities(id),
    token_hash     TEXT NOT NULL UNIQUE,
    active         INTEGER NOT NULL DEFAULT 1,
    issued_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credentials_elig ON credentials(eligibility_id);

-- 每个匿名代号至多一行：改投是覆盖更新，截止只留最后一次选择
CREATE TABLE IF NOT EXISTS votes (
    id           INTEGER PRIMARY KEY,
    ballot_id    INTEGER NOT NULL REFERENCES ballots(id),
    pseudonym_id INTEGER NOT NULL UNIQUE REFERENCES pseudonyms(id),
    choice       TEXT NOT NULL CHECK (choice IN ('yes', 'no', 'abstain')),
    updated_at   TEXT NOT NULL
);

-- 结算时刻的总数快照，结算后不再变化
CREATE TABLE IF NOT EXISTS results (
    ballot_id     INTEGER PRIMARY KEY REFERENCES ballots(id),
    yes_count     INTEGER NOT NULL,
    no_count      INTEGER NOT NULL,
    abstain_count INTEGER NOT NULL,
    settled_at    TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def tx(db_path: str):
    """串行化的立即写事务，保证并发操作按服务端实际接受顺序落库。"""
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
