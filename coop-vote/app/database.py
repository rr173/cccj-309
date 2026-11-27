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

-- 本地资金池：金额单位为整数分。
-- 恒等式：deposited = available + reserved + paid
--   available 可再分配；reserved 已被生效拨款保留（含受理中占用）；paid 已扣款出池
CREATE TABLE IF NOT EXISTS fund_pool (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    deposited_cents  INTEGER NOT NULL DEFAULT 0,
    reserved_cents   INTEGER NOT NULL DEFAULT 0,
    paid_cents       INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL
);

-- 提案：一场表决至多绑定一项；开放后冻结，不可新增/更改
-- kind: allocate=拨款提案, revoke=撤销提案；status 反映结算后的处理结果
CREATE TABLE IF NOT EXISTS proposals (
    id              INTEGER PRIMARY KEY,
    ballot_id       INTEGER NOT NULL UNIQUE REFERENCES ballots(id),
    kind            TEXT NOT NULL CHECK (kind IN ('allocate', 'revoke')),
    payee           TEXT,                          -- allocate 必填：固定收款方
    max_amount_cents INTEGER,                      -- allocate 必填：拨款上限（正整数分）
    grant_id        INTEGER,                       -- revoke 必填：要撤销的已生效拨款
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected',
                                      'insufficient_funds', 'grant_gone')),
    note            TEXT,
    executed_at     TEXT,
    CHECK (kind <> 'allocate' OR (payee IS NOT NULL AND max_amount_cents IS NOT NULL AND max_amount_cents > 0)),
    CHECK (kind <> 'revoke' OR grant_id IS NOT NULL)
);

-- 已生效拨款：批准时从资金池保留全部额度
CREATE TABLE IF NOT EXISTS grants (
    id               INTEGER PRIMARY KEY,
    proposal_id      INTEGER NOT NULL UNIQUE REFERENCES proposals(id),
    ballot_id        INTEGER NOT NULL UNIQUE REFERENCES ballots(id),
    payee            TEXT NOT NULL,
    max_amount_cents INTEGER NOT NULL CHECK (max_amount_cents > 0),
    reserved_cents   INTEGER NOT NULL,            -- 仍保留在拨款上的额度（含受理中占用）
    paid_cents       INTEGER NOT NULL DEFAULT 0,  -- 已确认扣款
    status           TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    created_at       TEXT NOT NULL,
    revoked_at       TEXT
);

-- 付款：同一拨款下请求号唯一；终态后任何重试都只回原记录
CREATE TABLE IF NOT EXISTS payments (
    id               INTEGER PRIMARY KEY,
    grant_id         INTEGER NOT NULL REFERENCES grants(id),
    request_no       TEXT NOT NULL,
    payee            TEXT NOT NULL,               -- 受理时固定的收款方快照
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    clearing_ref     TEXT NOT NULL UNIQUE,        -- 清算端幂等键，重启后也不变
    status           TEXT NOT NULL
                     CHECK (status IN ('accepted', 'paid', 'failed')),
    clearing_status  TEXT,                        -- 清算端最近一次返回的原始状态
    detail           TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (grant_id, request_no)
);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);

-- ---------------------------------------------------------------- 机具出借
-- 实物机具：asset_no 是管理员登记的独立编号；标签决定可满足的清单能力。
CREATE TABLE IF NOT EXISTS equipment (
    id          INTEGER PRIMARY KEY,
    asset_no    TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    retired_at  TEXT
);

CREATE TABLE IF NOT EXISTS equipment_tags (
    equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    tag          TEXT NOT NULL,
    PRIMARY KEY (equipment_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_equipment_tags_tag ON equipment_tags(tag);

-- 维修停用时段；撤销保留审计行，只把 status 改为 revoked。
CREATE TABLE IF NOT EXISTS maintenance_windows (
    id           INTEGER PRIMARY KEY,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    starts_at    TEXT NOT NULL,
    ends_at      TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'revoked')),
    created_at   TEXT NOT NULL,
    revoked_at   TEXT,
    CHECK (starts_at < ends_at)
);
CREATE INDEX IF NOT EXISTS idx_maintenance_equipment
    ON maintenance_windows(equipment_id, status);

-- 会员成套预约。状态：
-- waiting=等候；confirmed=可整套兑现但未锁序列号；cancelled=会员取消；
-- expired=未领取且已过结束时间；collected=已一次领齐；returned=全部实物已还。
CREATE TABLE IF NOT EXISTS reservations (
    id           INTEGER PRIMARY KEY,
    member_id    INTEGER NOT NULL REFERENCES members(id),
    starts_at    TEXT NOT NULL,
    ends_at      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'waiting'
                 CHECK (status IN ('waiting', 'confirmed', 'cancelled',
                                   'expired', 'collected', 'returned')),
    created_at   TEXT NOT NULL,
    confirmed_at TEXT,
    cancelled_at TEXT,
    expired_at   TEXT,
    collected_at TEXT,
    returned_at  TEXT,
    CHECK (starts_at < ends_at)
);
CREATE INDEX IF NOT EXISTS idx_reservations_member ON reservations(member_id, id);
CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status, id);

CREATE TABLE IF NOT EXISTS reservation_items (
    id             INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    ordinal        INTEGER NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    UNIQUE (reservation_id, ordinal)
);

CREATE TABLE IF NOT EXISTS reservation_item_tags (
    item_id INTEGER NOT NULL REFERENCES reservation_items(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (item_id, tag)
);

-- 未领取预约的当前可行性派工方案；它不是序列号锁，每次可用性变化可整体重算/改派。
CREATE TABLE IF NOT EXISTS reservation_assignments (
    item_id      INTEGER PRIMARY KEY REFERENCES reservation_items(id) ON DELETE CASCADE,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id),
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reservation_assignments_res
    ON reservation_assignments(reservation_id);
CREATE INDEX IF NOT EXISTS idx_reservation_assignments_equip
    ON reservation_assignments(equipment_id);

-- 实物交接后的逐件借还记录。未归还行才构成硬占用；已交接实物不再参与改派。
CREATE TABLE IF NOT EXISTS equipment_loans (
    id                  INTEGER PRIMARY KEY,
    reservation_id      INTEGER NOT NULL REFERENCES reservations(id),
    reservation_item_id INTEGER NOT NULL UNIQUE REFERENCES reservation_items(id),
    equipment_id        INTEGER NOT NULL REFERENCES equipment(id),
    checked_out_at      TEXT NOT NULL,
    due_at              TEXT NOT NULL,
    returned_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'out'
                        CHECK (status IN ('out', 'returned'))
);
CREATE INDEX IF NOT EXISTS idx_loans_equipment_open
    ON equipment_loans(equipment_id, returned_at);
CREATE INDEX IF NOT EXISTS idx_loans_reservation ON equipment_loans(reservation_id);
"""


def seed_fund_pool(db_path: str) -> None:
    """确保资金池单行存在（幂等，不改动既有金额）。"""
    from .service import utc_now

    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO fund_pool(id, deposited_cents, reserved_cents, paid_cents, updated_at) "
            "VALUES (1, 0, 0, 0, ?)",
            (utc_now(),),
        )
        conn.commit()
    finally:
        conn.close()


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
        # 轻量迁移：旧版本库没有 paid_cents 列时自动补上
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(fund_pool)").fetchall()}
        if "paid_cents" not in cols:
            conn.execute("ALTER TABLE fund_pool ADD COLUMN paid_cents INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()
    seed_fund_pool(db_path)


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
