"""资金池 / 提案 / 拨款 / 付款的业务逻辑。

金额一律为整数分。所有写操作沿用 ``database.tx`` 的全局锁 +
``BEGIN IMMEDIATE``，因此撤销与付款受理、多个提案争夺同一资金余额时，
结果与服务端实际受理（获锁）顺序完全一致。

付款生命周期：
    accepted（已受理、占用拨款额度）
      ├─ 清算端确认扣款 → paid（占用转已付）
      └─ 清算端明确失败 → failed（释放占用；拨款生效中退回拨款额度，
                           拨款已撤销则直接退回资金池可用额）

与清算端的网络调用都在数据库事务之外：付款先以 accepted 持久化，
再联系清算端；失联只保留 accepted，绝不判失败，之后由对账补齐终态。
"""
from __future__ import annotations

from typing import Any

from .clearing_client import ClearingClient, ClearingUnavailable
from .database import read, tx
from .security import generate_token
from .service import ServiceError, utc_now

PROPOSAL_KINDS = ("allocate", "revoke")
PAYMENT_STATUSES = ("accepted", "paid", "failed")


# ---------------------------------------------------------------- 资金池
def deposit(db_path: str, amount_cents: int) -> dict[str, Any]:
    """向本地资金池入金（演示/初始化用，只增不减）。"""
    if not isinstance(amount_cents, int) or amount_cents <= 0:
        raise ServiceError(422, "入金金额必须为正整数分")
    now = utc_now()
    with tx(db_path) as conn:
        conn.execute(
            "UPDATE fund_pool SET deposited_cents = deposited_cents + ?, updated_at=? WHERE id=1",
            (amount_cents, now),
        )
    return get_pool(db_path)


def get_pool(db_path: str) -> dict[str, Any]:
    with read(db_path) as conn:
        row = conn.execute("SELECT * FROM fund_pool WHERE id=1").fetchone()
        paid = row["paid_cents"] if "paid_cents" in row.keys() else 0
        return {
            "deposited_cents": row["deposited_cents"],
            "reserved_cents": row["reserved_cents"],
            "paid_cents": paid,
            # 恒等式：deposited = available + reserved + paid
            "available_cents": row["deposited_cents"] - row["reserved_cents"] - paid,
            "updated_at": row["updated_at"],
        }


# ---------------------------------------------------------------- 提案
def create_allocate_proposal(
    db_path: str, ballot_id: int, payee: str, max_amount_cents: int
) -> dict[str, Any]:
    if not payee or not payee.strip():
        raise ServiceError(422, "收款方不能为空")
    if not isinstance(max_amount_cents, int) or max_amount_cents <= 0:
        raise ServiceError(422, "拨款上限必须为正整数分")
    with tx(db_path) as conn:
        ballot = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        if ballot is None:
            raise ServiceError(404, "表决不存在")
        if ballot["status"] != "draft":
            raise ServiceError(409, "表决已开放或已结算，不能再绑定提案")
        existing = conn.execute(
            "SELECT id FROM proposals WHERE ballot_id=?", (ballot_id,)
        ).fetchone()
        if existing is not None:
            raise ServiceError(409, "一场表决至多绑定一项提案")
        cur = conn.execute(
            "INSERT INTO proposals(ballot_id, kind, payee, max_amount_cents, status) "
            "VALUES (?, 'allocate', ?, ?, 'pending')",
            (ballot_id, payee.strip(), max_amount_cents),
        )
        proposal_id = cur.lastrowid
    return get_proposal(db_path, proposal_id)


def create_revoke_proposal(db_path: str, ballot_id: int, grant_id: int) -> dict[str, Any]:
    with tx(db_path) as conn:
        ballot = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        if ballot is None:
            raise ServiceError(404, "表决不存在")
        if ballot["status"] != "draft":
            raise ServiceError(409, "表决已开放或已结算，不能再绑定提案")
        existing = conn.execute(
            "SELECT id FROM proposals WHERE ballot_id=?", (ballot_id,)
        ).fetchone()
        if existing is not None:
            raise ServiceError(409, "一场表决至多绑定一项提案")
        grant = conn.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
        if grant is None:
            raise ServiceError(404, f"拨款不存在: {grant_id}")
        if grant["status"] != "active":
            raise ServiceError(409, f"拨款 {grant_id} 已撤销，不能再提出撤销")
        cur = conn.execute(
            "INSERT INTO proposals(ballot_id, kind, grant_id, status) "
            "VALUES (?, 'revoke', ?, 'pending')",
            (ballot_id, grant_id),
        )
        proposal_id = cur.lastrowid
    return get_proposal(db_path, proposal_id)


def get_proposal(db_path: str, proposal_id: int) -> dict[str, Any]:
    with read(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(404, "提案不存在")
        return _proposal_out(conn, row)


def get_proposal_by_ballot(db_path: str, ballot_id: int) -> dict[str, Any] | None:
    with read(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM proposals WHERE ballot_id=?", (ballot_id,)
        ).fetchone()
        if row is None:
            return None
        return _proposal_out(conn, row)


def _proposal_out(conn, row) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "ballot_id": row["ballot_id"],
        "kind": row["kind"],
        "payee": row["payee"],
        "max_amount_cents": row["max_amount_cents"],
        "grant_id": row["grant_id"],
        "status": row["status"],
        "note": row["note"],
        "executed_at": row["executed_at"],
    }
    if row["kind"] == "allocate":
        grant = conn.execute(
            "SELECT id FROM grants WHERE proposal_id=?", (row["id"],)
        ).fetchone()
        out["resulting_grant_id"] = grant["id"] if grant else None
    return out


def apply_proposal_decision(conn, ballot_id: int, approved: bool, now: str) -> None:
    """结算事务内调用：根据决议执行提案。同一提案有状态守卫，不会重复执行。

    必须由调用方在 ``tx`` 事务内传入连接，保证“计票 + 决议执行”原子提交。
    """
    proposal = conn.execute(
        "SELECT * FROM proposals WHERE ballot_id=?", (ballot_id,)
    ).fetchone()
    if proposal is None:
        return  # 该表决未绑定提案，结算只固化票数
    if proposal["status"] != "pending":
        # 已执行过（正常路径不可达，防御重复拨款/退款）
        return

    if not approved:
        conn.execute(
            "UPDATE proposals SET status='rejected', note=?, executed_at=? WHERE id=?",
            ("赞成票未多于反对票，决议不通过", now, proposal["id"]),
        )
        return

    if proposal["kind"] == "allocate":
        amount = proposal["max_amount_cents"]
        pool = conn.execute("SELECT * FROM fund_pool WHERE id=1").fetchone()
        # 恒等式：available = deposited - reserved - paid
        available = (
            pool["deposited_cents"] - pool["reserved_cents"] - pool["paid_cents"]
        )
        if available < amount:
            # 票数照常公布；余额不足 → 拨款不生效，以后有余额也不会自行生效
            conn.execute(
                "UPDATE proposals SET status='insufficient_funds', note=?, executed_at=? WHERE id=?",
                (
                    f"资金池可用 {available} 分，不足保留 {amount} 分，拨款不生效",
                    now,
                    proposal["id"],
                ),
            )
            return
        # 为拨款保留全部额度
        conn.execute(
            "UPDATE fund_pool SET reserved_cents = reserved_cents + ?, updated_at=? WHERE id=1",
            (amount, now),
        )
        conn.execute(
            "INSERT INTO grants(proposal_id, ballot_id, payee, max_amount_cents, "
            "reserved_cents, paid_cents, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, 'active', ?)",
            (
                proposal["id"],
                ballot_id,
                proposal["payee"],
                amount,
                amount,
                now,
            ),
        )
        conn.execute(
            "UPDATE proposals SET status='approved', note=?, executed_at=? WHERE id=?",
            ("拨款已生效，全额保留", now, proposal["id"]),
        )
        return

    # 撤销提案通过
    grant = conn.execute(
        "SELECT * FROM grants WHERE id=?", (proposal["grant_id"],)
    ).fetchone()
    if grant is None or grant["status"] != "active":
        conn.execute(
            "UPDATE proposals SET status='grant_gone', note=?, executed_at=? WHERE id=?",
            ("目标拨款不存在或已撤销，无可执行内容", now, proposal["id"]),
        )
        return
    # 受理中付款仍占用的额度必须保留（它们还可能扣款成功），
    # 只把“未被受理中付款锁住”的保留额立即退回资金池。
    held = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS n FROM payments "
        "WHERE grant_id=? AND status='accepted'",
        (grant["id"],),
    ).fetchone()["n"]
    refund = grant["reserved_cents"] - held
    if refund > 0:
        conn.execute(
            "UPDATE fund_pool SET reserved_cents = reserved_cents - ?, updated_at=? WHERE id=1",
            (refund, now),
        )
    # 拨款账面只保留受理中占用；这些付款终态时再按其结果处理
    conn.execute(
        "UPDATE grants SET status='revoked', reserved_cents=?, revoked_at=? WHERE id=?",
        (held, now, grant["id"]),
    )
    conn.execute(
        "UPDATE proposals SET status='approved', note=?, executed_at=? WHERE id=?",
        (
            f"拨款已撤销，退回未占用额度 {refund} 分；已付款不追回，受理中款仍可完成",
            now,
            proposal["id"],
        ),
    )


# ---------------------------------------------------------------- 拨款查询
def list_grants(db_path: str) -> list[dict[str, Any]]:
    with read(db_path) as conn:
        rows = conn.execute("SELECT * FROM grants ORDER BY id").fetchall()
        return [_grant_out(conn, r) for r in rows]


def get_grant(db_path: str, grant_id: int) -> dict[str, Any]:
    with read(db_path) as conn:
        row = conn.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
        if row is None:
            raise ServiceError(404, "拨款不存在")
        return _grant_out(conn, row)


def _grant_out(conn, row) -> dict[str, Any]:
    accepted = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS n FROM payments "
        "WHERE grant_id=? AND status='accepted'",
        (row["id"],),
    ).fetchone()["n"]
    return {
        "id": row["id"],
        "ballot_id": row["ballot_id"],
        "proposal_id": row["proposal_id"],
        "payee": row["payee"],
        "status": row["status"],
        "max_amount_cents": row["max_amount_cents"],
        "reserved_cents": row["reserved_cents"],
        "paid_cents": row["paid_cents"],
        "accepted_pending_cents": accepted,
        # 还能新受理多少付款
        "available_for_payment_cents": row["reserved_cents"] - accepted,
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
        "effective": row["status"] == "active",
    }


# ---------------------------------------------------------------- 付款
def submit_payment(
    db_path: str,
    clearing: ClearingClient,
    grant_id: int,
    request_no: str,
    amount_cents: int,
) -> dict[str, Any]:
    """管理员在生效拨款额度内向固定收款方发起一笔付款。

    - 同一 (grant, request_no) 重试：返回原付款及其当前状态，不重复占用/扣款；
      若原付款仍在受理中，顺带做一次幂等对账，让重试能拿到最新状态。
    - 已受理的请求号不得改金额重用 → 409。
    - 受理先落库（accepted、占用额度），再调用清算端；
      清算端失联时付款保持 accepted，服务重启也不丢失。
    """
    if not request_no or not request_no.strip():
        raise ServiceError(422, "请求号不能为空")
    if not isinstance(amount_cents, int) or amount_cents <= 0:
        raise ServiceError(422, "单笔付款金额必须为正整数分")
    request_no = request_no.strip()
    now = utc_now()

    # 第一步：事务内受理（或命中幂等原记录）
    with tx(db_path) as conn:
        grant = conn.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
        if grant is None:
            raise ServiceError(404, "拨款不存在")

        existing = conn.execute(
            "SELECT * FROM payments WHERE grant_id=? AND request_no=?",
            (grant_id, request_no),
        ).fetchone()
        if existing is not None:
            # 幂等优先：哪怕拨款已撤销，原请求号的重试也要能补齐/返回原付款
            if existing["amount_cents"] != amount_cents:
                raise ServiceError(
                    409,
                    f"请求号 {request_no} 已受理（金额 {existing['amount_cents']} 分），"
                    "不得改金额重用",
                )
            payment_id = existing["id"]
            ref = existing["clearing_ref"]
            already_accepted = existing["status"] == "accepted"
        else:
            if grant["status"] != "active":
                raise ServiceError(409, "拨款已撤销，不再受理新付款")
            held = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS n FROM payments "
                "WHERE grant_id=? AND status='accepted'",
                (grant_id,),
            ).fetchone()["n"]
            if held + amount_cents > grant["reserved_cents"]:
                raise ServiceError(
                    409,
                    f"超出拨款可用额度：受理中占用 {held} 分，本笔 {amount_cents} 分，"
                    f"拨款保留 {grant['reserved_cents']} 分",
                )
            ref = f"pay_{generate_token(24)}"
            cur = conn.execute(
                "INSERT INTO payments(grant_id, request_no, payee, amount_cents, "
                "clearing_ref, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?)",
                (grant_id, request_no, grant["payee"], amount_cents, ref, now, now),
            )
            payment_id = cur.lastrowid
            already_accepted = True

    # 第二步：事务外联系清算端。失联不判失败，保留 accepted 等待对账。
    if already_accepted:
        try:
            result = clearing.submit_payment(ref, grant["payee"], amount_cents)
        except ClearingUnavailable:
            return get_payment(db_path, payment_id)
        _apply_clearing_result(db_path, payment_id, result)

    return get_payment(db_path, payment_id)


def _apply_clearing_result(db_path: str, payment_id: int, result: dict[str, Any]) -> None:
    """把清算端的明确结果落库（幂等，状态只向前推进）。"""
    remote_status = result.get("status")
    now = utc_now()
    with tx(db_path) as conn:
        payment = conn.execute(
            "SELECT * FROM payments WHERE id=?", (payment_id,)
        ).fetchone()
        if payment is None:
            return
        if payment["status"] != "accepted":
            return  # 已有终态，幂等忽略
        if remote_status == "paid":
            grant = conn.execute(
                "SELECT * FROM grants WHERE id=?", (payment["grant_id"],)
            ).fetchone()
            # 占用转已付：拨款保留额下降、已付额上升；
            # 资金池保留额同步下降，并计入已付（钱已出池），可用额不变
            conn.execute(
                "UPDATE grants SET reserved_cents = reserved_cents - ?, "
                "paid_cents = paid_cents + ? WHERE id=?",
                (payment["amount_cents"], payment["amount_cents"], grant["id"]),
            )
            conn.execute(
                "UPDATE fund_pool SET reserved_cents = reserved_cents - ?, "
                "paid_cents = paid_cents + ?, updated_at=? WHERE id=1",
                (payment["amount_cents"], payment["amount_cents"], now),
            )
            conn.execute(
                "UPDATE payments SET status='paid', clearing_status='paid', "
                "detail=?, updated_at=? WHERE id=?",
                (result.get("detail", "扣款成功"), now, payment_id),
            )
        elif remote_status == "failed":
            grant = conn.execute(
                "SELECT * FROM grants WHERE id=?", (payment["grant_id"],)
            ).fetchone()
            if grant["status"] == "active":
                # 拨款生效中：占用退回拨款额度，之后可用同一请求号之外的新请求重试
                conn.execute(
                    "UPDATE payments SET status='failed', clearing_status='failed', "
                    "detail=?, updated_at=? WHERE id=?",
                    (result.get("detail", "扣款失败"), now, payment_id),
                )
            else:
                # 拨款已撤销：占用额退回资金池，而不是恢复已撤销的拨款额度
                conn.execute(
                    "UPDATE grants SET reserved_cents = reserved_cents - ? WHERE id=?",
                    (payment["amount_cents"], grant["id"]),
                )
                conn.execute(
                    "UPDATE fund_pool SET reserved_cents = reserved_cents - ?, updated_at=? WHERE id=1",
                    (payment["amount_cents"], now),
                )
                conn.execute(
                    "UPDATE payments SET status='failed', clearing_status='failed', "
                    "detail=?, updated_at=? WHERE id=?",
                    (result.get("detail", "扣款失败") + "（拨款已撤销，占用退回资金池）", now, payment_id),
                )
        # 其它未知状态：保持 accepted，等待下一次对账


def reconcile_payment(db_path: str, clearing: ClearingClient, payment_id: int) -> dict[str, Any]:
    """凭清算端查询补齐单笔付款终态。失联时保持现状并原样返回。"""
    with read(db_path) as conn:
        payment = conn.execute(
            "SELECT * FROM payments WHERE id=?", (payment_id,)
        ).fetchone()
        if payment is None:
            raise ServiceError(404, "付款不存在")
        ref = payment["clearing_ref"]
        if payment["status"] != "accepted":
            return _payment_out(payment)
    try:
        result = clearing.get_payment(ref)
    except ClearingUnavailable:
        return get_payment(db_path, payment_id)
    _apply_clearing_result(db_path, payment_id, result)
    return get_payment(db_path, payment_id)


def reconcile_pending(db_path: str, clearing: ClearingClient) -> dict[str, Any]:
    """补齐所有受理中付款（服务重启后、清算端恢复后调用）。"""
    with read(db_path) as conn:
        pending = conn.execute(
            "SELECT id FROM payments WHERE status='accepted' ORDER BY id"
        ).fetchall()
    ids = [r["id"] for r in pending]
    for pid in ids:
        reconcile_payment(db_path, clearing, pid)
    return {"checked": len(ids), "payments": [get_payment(db_path, pid) for pid in ids]}


def get_payment(db_path: str, payment_id: int) -> dict[str, Any]:
    with read(db_path) as conn:
        row = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if row is None:
            raise ServiceError(404, "付款不存在")
        return _payment_out(row)


def get_payment_by_request(db_path: str, grant_id: int, request_no: str) -> dict[str, Any]:
    with read(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM payments WHERE grant_id=? AND request_no=?",
            (grant_id, request_no),
        ).fetchone()
        if row is None:
            raise ServiceError(404, "该请求号下无付款记录")
        return _payment_out(row)


def list_payments(db_path: str, grant_id: int | None = None) -> list[dict[str, Any]]:
    with read(db_path) as conn:
        if grant_id is None:
            rows = conn.execute("SELECT * FROM payments ORDER BY id").fetchall()
        else:
            grant = conn.execute("SELECT id FROM grants WHERE id=?", (grant_id,)).fetchone()
            if grant is None:
                raise ServiceError(404, "拨款不存在")
            rows = conn.execute(
                "SELECT * FROM payments WHERE grant_id=? ORDER BY id", (grant_id,)
            ).fetchall()
        return [_payment_out(r) for r in rows]


def _payment_out(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "grant_id": row["grant_id"],
        "request_no": row["request_no"],
        "payee": row["payee"],
        "amount_cents": row["amount_cents"],
        "status": row["status"],
        "clearing_ref": row["clearing_ref"],
        "detail": row["detail"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
