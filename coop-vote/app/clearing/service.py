"""清算模拟端业务逻辑：去重受理、扣款、结果查询与故障注入。

付款状态机：accepted（已受理）→ paid（已扣款）/ failed（扣款失败）。
同一 ``ref`` 的重复提交永远返回首次创建的流水，绝不产生第二笔扣款。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import read, tx


class ClearingError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class ConnectionDropped(Exception):
    """模拟“服务端已完成扣款，但响应在回程中断开”。

    真实环境里对应连接重置/读超时；抛出后 ASGI 服务端会中断响应，
    调用方无法得知结果，只能稍后凭 ref 查询或重试。
    """


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_offline(conn) -> bool:
    return conn.execute("SELECT offline FROM sim_controls WHERE id=1").fetchone()["offline"] == 1


def health(db_path: str) -> dict[str, Any]:
    with read(db_path) as conn:
        offline = _is_offline(conn)
    if offline:
        raise ClearingError(503, "清算服务模拟宕机中")
    return {"status": "ok"}


def submit_payment(db_path: str, ref: str, payee: str, amount_cents: int) -> dict[str, Any]:
    """受理并尝试扣款。

    幂等：ref 已存在时直接返回原流水（含其当前状态），不重复扣款。
    """
    if not isinstance(amount_cents, int) or amount_cents <= 0:
        raise ClearingError(422, "金额必须为正整数分")
    if not ref or not payee:
        raise ClearingError(422, "ref 与 payee 不能为空")

    now = utc_now()
    with tx(db_path) as conn:
        if _is_offline(conn):
            raise ClearingError(503, "清算服务模拟宕机中")

        existing = conn.execute(
            "SELECT * FROM clearing_payments WHERE ref=?", (ref,)
        ).fetchone()
        if existing is not None:
            # 去重：原封不动返回既有流水，绝不再扣一次
            return dict(existing)

        controls = conn.execute(
            "SELECT drop_after_debit, fail_next FROM sim_controls WHERE id=1"
        ).fetchone()
        drop = controls["drop_after_debit"] == 1
        fail = controls["fail_next"] == 1
        # fail_next 是一次性开关
        conn.execute("UPDATE sim_controls SET fail_next=0 WHERE id=1")

        if fail:
            cur = conn.execute(
                "INSERT INTO clearing_payments(ref, payee, amount_cents, status, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, 'failed', ?, ?, ?)",
                (ref, payee, amount_cents, "模拟扣款失败", now, now),
            )
            row = conn.execute(
                "SELECT * FROM clearing_payments WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)

        # 正常路径：受理即扣款（本地模拟没有异步环节），先落库提交
        cur = conn.execute(
            "INSERT INTO clearing_payments(ref, payee, amount_cents, status, detail, created_at, updated_at) "
            "VALUES (?, ?, ?, 'paid', ?, ?, ?)",
            (ref, payee, amount_cents, "扣款成功", now, now),
        )
        row = dict(
            conn.execute("SELECT * FROM clearing_payments WHERE id=?", (cur.lastrowid,)).fetchone()
        )

    # 事务已提交、钱已扣；此时模拟响应断开（在锁外抛错，状态已持久化）
    if drop:
        raise ConnectionDropped(f"付款 {ref} 已扣款，但响应链路断开")
    return row


def get_payment(db_path: str, ref: str) -> dict[str, Any]:
    with read(db_path) as conn:
        if _is_offline(conn):
            raise ClearingError(503, "清算服务模拟宕机中")
        row = conn.execute(
            "SELECT * FROM clearing_payments WHERE ref=?", (ref,)
        ).fetchone()
    if row is None:
        raise ClearingError(404, "清算端无此付款流水")
    return dict(row)


def set_sim_control(
    db_path: str,
    offline: bool | None = None,
    drop_after_debit: bool | None = None,
    fail_next: bool | None = None,
) -> dict[str, Any]:
    with tx(db_path) as conn:
        if offline is not None:
            conn.execute("UPDATE sim_controls SET offline=? WHERE id=1", (1 if offline else 0,))
        if drop_after_debit is not None:
            conn.execute(
                "UPDATE sim_controls SET drop_after_debit=? WHERE id=1",
                (1 if drop_after_debit else 0,),
            )
        if fail_next is not None:
            conn.execute(
                "UPDATE sim_controls SET fail_next=? WHERE id=1",
                (1 if fail_next else 0,),
            )
        row = conn.execute("SELECT * FROM sim_controls WHERE id=1").fetchone()
        return {
            "offline": row["offline"] == 1,
            "drop_after_debit": row["drop_after_debit"] == 1,
            "fail_next": row["fail_next"] == 1,
        }
