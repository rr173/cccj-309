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

-- ---------------------------------------------------------------- 批次库存
-- case_count 是仍在库、尚未交货的箱数（含已备货占用）；
-- available = case_count - status='prepared' 单据上的分配箱数。
CREATE TABLE IF NOT EXISTS inv_batches (
    id              INTEGER PRIMARY KEY,
    batch_no        TEXT NOT NULL UNIQUE,
    variety         TEXT NOT NULL,
    origin          TEXT NOT NULL DEFAULT 'inbound'
                    CHECK (origin IN ('inbound', 'composed')),
    initial_cases   INTEGER NOT NULL CHECK (initial_cases > 0),
    case_count      INTEGER NOT NULL CHECK (case_count >= 0),
    delivered_cases INTEGER NOT NULL DEFAULT 0 CHECK (delivered_cases >= 0),
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inv_batches_variety ON inv_batches(variety);

-- 一次“从一个或多个批次拆出并合成新批次”的操作及实际投入箱数。
CREATE TABLE IF NOT EXISTS inv_compositions (
    id           INTEGER PRIMARY KEY,
    new_batch_id INTEGER NOT NULL UNIQUE REFERENCES inv_batches(id),
    total_cases  INTEGER NOT NULL CHECK (total_cases > 0),
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inv_composition_items (
    id             INTEGER PRIMARY KEY,
    composition_id INTEGER NOT NULL REFERENCES inv_compositions(id) ON DELETE CASCADE,
    source_batch_id INTEGER NOT NULL REFERENCES inv_batches(id),
    cases          INTEGER NOT NULL CHECK (cases > 0),
    UNIQUE (composition_id, source_batch_id)
);

-- 来源闭包：(batch_id, source_batch_id) 表示 batch_id 的每一箱都接触过
-- source_batch_id；每个批次也插入自反行，便于一次查出目标批次及其全部后代。
CREATE TABLE IF NOT EXISTS inv_batch_sources (
    batch_id        INTEGER NOT NULL REFERENCES inv_batches(id) ON DELETE CASCADE,
    source_batch_id INTEGER NOT NULL REFERENCES inv_batches(id),
    PRIMARY KEY (batch_id, source_batch_id)
);
CREATE INDEX IF NOT EXISTS idx_inv_batch_sources_source
    ON inv_batch_sources(source_batch_id);

-- 出库单：pending=待备货（分配行只是预案，不占用）；prepared=已备货/已占用；
-- delivered=整单已交货；cancelled=未交货取消并释放占用。
CREATE TABLE IF NOT EXISTS inv_outbound_orders (
    id           INTEGER PRIMARY KEY,
    order_no     TEXT UNIQUE,
    buyer_name   TEXT NOT NULL,
    buyer_code   TEXT NOT NULL DEFAULT '',
    variety      TEXT NOT NULL,
    total_cases  INTEGER NOT NULL CHECK (total_cases > 0),
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'prepared', 'delivered', 'cancelled')),
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    prepared_at  TEXT,
    delivered_at TEXT,
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_inv_orders_status ON inv_outbound_orders(status, id);
CREATE INDEX IF NOT EXISTS idx_inv_orders_buyer ON inv_outbound_orders(buyer_name);

CREATE TABLE IF NOT EXISTS inv_outbound_allocations (
    id                 INTEGER PRIMARY KEY,
    order_id           INTEGER NOT NULL REFERENCES inv_outbound_orders(id) ON DELETE CASCADE,
    batch_id           INTEGER NOT NULL REFERENCES inv_batches(id),
    cases              INTEGER NOT NULL CHECK (cases > 0),
    batch_no_snapshot  TEXT NOT NULL,
    variety_snapshot   TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (order_id, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_inv_allocations_batch
    ON inv_outbound_allocations(batch_id);

-- 交货时固化的实际出库明细；即使后续审计/展示逻辑变化，召回仍以这些行为准。
CREATE TABLE IF NOT EXISTS inv_deliveries (
    id           INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL UNIQUE REFERENCES inv_outbound_orders(id),
    delivered_at TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS inv_delivery_items (
    id            INTEGER PRIMARY KEY,
    delivery_id   INTEGER NOT NULL REFERENCES inv_deliveries(id) ON DELETE CASCADE,
    order_id      INTEGER NOT NULL REFERENCES inv_outbound_orders(id),
    allocation_id INTEGER REFERENCES inv_outbound_allocations(id),
    batch_id      INTEGER NOT NULL REFERENCES inv_batches(id),
    batch_no      TEXT NOT NULL,
    variety       TEXT NOT NULL,
    cases         INTEGER NOT NULL CHECK (cases > 0),
    UNIQUE (order_id, allocation_id),
    UNIQUE (delivery_id, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_inv_delivery_items_batch
    ON inv_delivery_items(batch_id);

-- 独立质量事件；同一目标可登记多个事件，全部解除后隔离才结束。
CREATE TABLE IF NOT EXISTS inv_quality_events (
    id              INTEGER PRIMARY KEY,
    event_no        TEXT UNIQUE,
    target_batch_id INTEGER NOT NULL REFERENCES inv_batches(id),
    title           TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    severity        TEXT NOT NULL DEFAULT 'normal',
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'resolved')),
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolution_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_inv_quality_events_target
    ON inv_quality_events(target_batch_id, status);

-- 事件登记时形成的召回单（事件 × 已交货订单唯一）和受影响交付明细快照。
CREATE TABLE IF NOT EXISTS inv_recall_cases (
    id             INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL REFERENCES inv_quality_events(id) ON DELETE CASCADE,
    order_id       INTEGER NOT NULL REFERENCES inv_outbound_orders(id),
    order_no       TEXT NOT NULL,
    buyer_name     TEXT NOT NULL,
    contacted      INTEGER NOT NULL DEFAULT 0,
    contact_note   TEXT NOT NULL DEFAULT '',
    contacted_by   TEXT NOT NULL DEFAULT '',
    contact_count  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    contacted_at   TEXT,
    last_contacted_at TEXT,
    UNIQUE (event_id, order_id)
);
CREATE INDEX IF NOT EXISTS idx_inv_recall_cases_contact
    ON inv_recall_cases(event_id, contacted);

CREATE TABLE IF NOT EXISTS inv_recall_items (
    id               INTEGER PRIMARY KEY,
    case_id          INTEGER NOT NULL REFERENCES inv_recall_cases(id) ON DELETE CASCADE,
    event_id         INTEGER NOT NULL REFERENCES inv_quality_events(id) ON DELETE CASCADE,
    order_id         INTEGER NOT NULL REFERENCES inv_outbound_orders(id),
    delivery_item_id INTEGER NOT NULL REFERENCES inv_delivery_items(id),
    batch_id         INTEGER NOT NULL REFERENCES inv_batches(id),
    batch_no         TEXT NOT NULL,
    variety          TEXT NOT NULL,
    cases            INTEGER NOT NULL CHECK (cases > 0),
    UNIQUE (event_id, delivery_item_id)
);
CREATE INDEX IF NOT EXISTS idx_inv_recall_items_event ON inv_recall_items(event_id);

-- 审计：质量事件把哪些已备货单整单退回待备货。
CREATE TABLE IF NOT EXISTS inv_event_order_actions (
    id         INTEGER PRIMARY KEY,
    event_id   INTEGER NOT NULL REFERENCES inv_quality_events(id) ON DELETE CASCADE,
    order_id   INTEGER NOT NULL REFERENCES inv_outbound_orders(id),
    action     TEXT NOT NULL CHECK (action IN ('returned_to_pending')),
    created_at TEXT NOT NULL,
    UNIQUE (event_id, order_id, action)
);
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
