"""成套机具预约、可行性重算、实物交接与逐件归还。

核心不变量
----------
1. 一张预约单的全部要求必须同时满足；同一台实物在同一套中不能覆盖两件。
2. 确认只代表“当前存在一种整套派工方案”，不锁死序列号；未领取单在每次
   可用性变化/状态查看时都可能被重算或改派。
3. 已交接的 ``equipment_loans`` 行是硬占用，不能被自动改派；未还且已过
   约定到期时间的机具，在实际归还前完全不能再承诺。
4. 所有写操作都走 :func:`app.database.tx` 的进程内全局锁与
   ``BEGIN IMMEDIATE``，因此并发请求有确定的服务端接受顺序。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .database import tx
from . import service
from .service import ServiceError

ACTIVE_RESERVATION_STATUSES = ("waiting", "confirmed")
TERMINAL_PICKUP_STATUSES = ("collected", "returned")
STATUS_LABELS = {
    "waiting": "等候",
    "confirmed": "确认",
    "cancelled": "已取消",
    "expired": "失效",
    "collected": "已领取",
    "returned": "已归还",
    "overdue": "逾期",
}


# --------------------------------------------------------------------- 时间工具
def utc_now() -> str:
    """转发到 service 时钟；测试既可 patch app.equipment.utc_now，
    也可 patch app.service.utc_now 来统一推进服务端时间。"""
    return service.utc_now()


def _now_dt() -> datetime:
    return _dt(utc_now())


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ServiceError(422, "时间必须是 ISO 8601 字符串或 datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """左闭右开区间重叠判定；端点相接不算重叠。"""
    return start_a < end_b and start_b < end_a


def _clean_asset_no(value: str) -> str:
    if value is None:
        raise ServiceError(422, "机具独立编号不能为空")
    asset_no = str(value).strip()
    if not asset_no:
        raise ServiceError(422, "机具独立编号不能为空")
    if len(asset_no) > 64:
        raise ServiceError(422, "机具编号最长 64 个字符")
    return asset_no


def _clean_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if not isinstance(tags, (list, tuple, set, frozenset)):
        raise ServiceError(422, "标签必须是字符串数组")
    result: list[str] = []
    for raw in tags:
        if not isinstance(raw, str):
            raise ServiceError(422, "标签必须是字符串")
        tag = raw.strip()
        if not tag:
            raise ServiceError(422, "标签不能为空")
        if len(tag) > 64:
            raise ServiceError(422, "单个标签最长 64 个字符")
        if tag not in result:
            result.append(tag)
    return result


# --------------------------------------------------------------------- 数据装载
@dataclass
class Requirement:
    item_id: int
    reservation_id: int
    ordinal: int
    start: datetime
    end: datetime
    tags: frozenset[str]


@dataclass
class Availability:
    now: datetime
    tags_by_equipment: dict[int, frozenset[str]]
    blocks: dict[int, list[tuple[datetime, datetime]]] = field(default_factory=dict)
    overdue: set[int] = field(default_factory=set)

    @classmethod
    def load(cls, conn, now: datetime) -> "Availability":
        tags_by_equipment: dict[int, frozenset[str]] = {}
        for row in conn.execute("SELECT equipment_id, tag FROM equipment_tags").fetchall():
            tags_by_equipment.setdefault(row["equipment_id"], frozenset())
            # frozenset 不可变，需要重新合并。
            tags_by_equipment[row["equipment_id"]] = (
                tags_by_equipment[row["equipment_id"]] | {row["tag"]}
            )

        avail = cls(now=now, tags_by_equipment=tags_by_equipment)
        for row in conn.execute(
            "SELECT equipment_id, starts_at, ends_at FROM maintenance_windows "
            "WHERE status='active'"
        ).fetchall():
            avail.blocks.setdefault(row["equipment_id"], []).append(
                (_dt(row["starts_at"]), _dt(row["ends_at"]))
            )

        for row in conn.execute(
            "SELECT id, equipment_id, checked_out_at, due_at FROM equipment_loans "
            "WHERE returned_at IS NULL"
        ).fetchall():
            due = _dt(row["due_at"])
            if now >= due:
                # 到期未还：在真正归还前，该实物不能再对任何未来时段作出承诺。
                avail.overdue.add(row["equipment_id"])
            else:
                # 未到期：按约定结束时间预计可再次使用，因此只硬占用到 due。
                avail.blocks.setdefault(row["equipment_id"], []).append(
                    (_dt(row["checked_out_at"]), due)
                )
        return avail

    def available(
        self,
        equipment_id: int,
        required_tags: frozenset[str],
        start: datetime,
        end: datetime,
        extra_blocks: dict[int, list[tuple[datetime, datetime]]] | None = None,
    ) -> bool:
        if equipment_id in self.overdue:
            return False
        equipment_tags = self.tags_by_equipment.get(equipment_id, frozenset())
        if not required_tags <= equipment_tags:
            return False
        for block_start, block_end in self.blocks.get(equipment_id, []):
            if _overlap(start, end, block_start, block_end):
                return False
        if extra_blocks:
            for block_start, block_end in extra_blocks.get(equipment_id, []):
                if _overlap(start, end, block_start, block_end):
                    return False
        return True


def _load_order(conn, row, now: datetime) -> dict[str, Any]:
    """装载预约单及其清单；未领取单随当前时间把借用区间收缩为剩余借期。"""
    start = _dt(row["starts_at"])
    end = _dt(row["ends_at"])
    effective_start = max(start, now) if row["status"] in ACTIVE_RESERVATION_STATUSES else start
    items: list[dict[str, Any]] = []
    item_rows = conn.execute(
        "SELECT id, ordinal, label FROM reservation_items "
        "WHERE reservation_id=? ORDER BY ordinal, id",
        (row["id"],),
    ).fetchall()
    for item_row in item_rows:
        tag_rows = conn.execute(
            "SELECT tag FROM reservation_item_tags WHERE item_id=? ORDER BY tag",
            (item_row["id"],),
        ).fetchall()
        tags = frozenset(r["tag"] for r in tag_rows)
        items.append(
            {
                "item_id": item_row["id"],
                "ordinal": item_row["ordinal"],
                "label": item_row["label"],
                "tags": tags,
            }
        )
    return {
        "id": row["id"],
        "member_id": row["member_id"],
        "start": start,
        "end": end,
        "effective_start": effective_start,
        "effective_end": end,
        "status": row["status"],
        "items": items,
    }


def _load_active_orders(conn, now: datetime) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM reservations WHERE status IN ('waiting','confirmed') ORDER BY id"
    ).fetchall()
    return {row["id"]: _load_order(conn, row, now) for row in rows}


# --------------------------------------------------------------------- 派工求解
def _segment_matching_possible(
    reqs: list[Requirement],
    candidates: dict[int, list[int]],
) -> bool:
    """必要的 clique/Hall 剪枝：每个时间原子段上的在借要求必须能一一匹配实物。

    同一时间段上的所有要求两两重叠，因此需要不同机具。这里用二分匹配尽早
    排除“同一时刻能力/数量不足”，避免回溯穷举所有排列。
    """
    if not reqs:
        return True
    points = sorted({p for r in reqs for p in (r.start, r.end)})
    for left, right in zip(points, points[1:]):
        active = [r for r in reqs if r.start <= left and r.end > left]
        if not active:
            continue
        match_to_req: dict[int, int] = {}

        def augment(req: Requirement, seen: set[int]) -> bool:
            for equipment_id in candidates[req.item_id]:
                if equipment_id in seen:
                    continue
                seen.add(equipment_id)
                owner = match_to_req.get(equipment_id)
                if owner is None:
                    match_to_req[equipment_id] = req.item_id
                    return True
                owner_req = next(x for x in active if x.item_id == owner)
                if augment(owner_req, seen):
                    match_to_req[equipment_id] = req.item_id
                    return True
            return False

        for req in active:
            if not augment(req, set()):
                return False
    return True


def solve_requirements(
    reqs: list[Requirement],
    ctx: Availability,
    extra_blocks: dict[int, list[tuple[datetime, datetime]]] | None = None,
    equipment_ids: Iterable[int] | None = None,
) -> dict[int, int] | None:
    """为一批要求寻找“同一实物覆盖整段、重叠要求不共用”的派工方案。

    返回 ``{reservation_item_id: equipment_id}``；无法整套满足时返回 None。
    采用 MRV 回溯。规模为合作社本地机具/预约量，且有时间段二分匹配剪枝。
    """
    if not reqs:
        return {}

    if equipment_ids is None:
        equipment_ids = list(ctx.tags_by_equipment.keys())
    equipment_ids = list(equipment_ids)

    candidates: dict[int, list[int]] = {}
    eligibility_count: dict[int, int] = {eid: 0 for eid in equipment_ids}
    for req in reqs:
        options = [
            eid
            for eid in equipment_ids
            if ctx.available(
                eid, req.tags, req.start, req.end, extra_blocks=extra_blocks
            )
        ]
        if not options:
            return None
        candidates[req.item_id] = options
        for eid in options:
            eligibility_count[eid] += 1

    by_id = {req.item_id: req for req in reqs}
    neighbor_ids: dict[int, set[int]] = {}
    for req in reqs:
        neighbors: set[int] = set()
        for other in reqs:
            if other.item_id == req.item_id:
                continue
            if _overlap(req.start, req.end, other.start, other.end):
                neighbors.add(other.item_id)
        neighbor_ids[req.item_id] = neighbors

    # 时间重叠图的不同连通分量在时间上互不牵连，可以分别求解后共用同一批
    # 机具编号（不同分量天然不重叠）。这避免大量无关预约把回溯规模放大。
    components: list[list[Requirement]] = []
    seen_components: set[int] = set()
    for req in reqs:
        if req.item_id in seen_components:
            continue
        stack = [req.item_id]
        component_ids: set[int] = set()
        while stack:
            item_id = stack.pop()
            if item_id in component_ids:
                continue
            component_ids.add(item_id)
            for neighbor_id in neighbor_ids[item_id]:
                if neighbor_id not in component_ids:
                    stack.append(neighbor_id)
        seen_components |= component_ids
        components.append([by_id[item_id] for item_id in component_ids])

    result: dict[int, int] = {}
    for component in components:
        component_ids_set = {req.item_id for req in component}
        if not _segment_matching_possible(component, candidates):
            return None

        assignment: dict[int, int] = {}

        def legal_options(item_id: int) -> list[int]:
            used_by_neighbors = {
                assignment[nid]
                for nid in neighbor_ids[item_id]
                if nid in assignment
            }
            options = [
                eid for eid in candidates[item_id] if eid not in used_by_neighbors
            ]
            # 优先派给“还能服务更少其他要求”的机具，尽量保留通用/热门实物。
            options.sort(
                key=lambda eid: (
                    eligibility_count[eid],
                    len(ctx.tags_by_equipment.get(eid, frozenset())),
                    eid,
                )
            )
            return options

        def dfs() -> bool:
            if len(assignment) == len(component_ids_set):
                return True

            unassigned = [req.item_id for req in component if req.item_id not in assignment]
            best_id: int | None = None
            best_options: list[int] | None = None
            for item_id in unassigned:
                options = legal_options(item_id)
                if not options:
                    return False
                if best_options is None or len(options) < len(best_options):
                    best_id = item_id
                    best_options = options
                    if len(options) == 1:
                        break

            assert best_id is not None and best_options is not None
            for equipment_id in best_options:
                assignment[best_id] = equipment_id
                if dfs():
                    return True
                del assignment[best_id]
            return False

        if not dfs():
            return None
        result.update(assignment)

    # 确认返回值可直接 JSON 化。
    return {item_id: equipment_id for item_id, equipment_id in result.items()}


def _reqs_from_orders(orders: list[dict[str, Any]]) -> list[Requirement]:
    reqs: list[Requirement] = []
    for order in orders:
        for item in order["items"]:
            reqs.append(
                Requirement(
                    item_id=item["item_id"],
                    reservation_id=order["id"],
                    ordinal=item["ordinal"],
                    start=order["effective_start"],
                    end=order["effective_end"],
                    tags=item["tags"],
                )
            )
    return reqs


def _solve_orders(
    orders: list[dict[str, Any]],
    ctx: Availability,
    extra_blocks: dict[int, list[tuple[datetime, datetime]]] | None = None,
) -> dict[int, int] | None:
    return solve_requirements(_reqs_from_orders(orders), ctx, extra_blocks)


def _extend_solution(
    order: dict[str, Any],
    retained_orders: list[dict[str, Any]],
    existing_solution: dict[int, int],
    ctx: Availability,
) -> dict[int, int] | None:
    """在不改动此前已保留订单派工的前提下尝试加入新单。

    成功时返回新增 item_id -> equipment_id；失败后调用方仍可执行允许整体
    改派的全量求解。这个快速路径让大量时间互不重叠的预约不必反复全量回溯。
    """
    new_reqs = _reqs_from_orders([order])
    existing_reqs = _reqs_from_orders(retained_orders)
    if not new_reqs:
        return {}

    candidates: dict[int, list[int]] = {}
    for req in new_reqs:
        options: list[int] = []
        for equipment_id in ctx.tags_by_equipment.keys():
            if not ctx.available(equipment_id, req.tags, req.start, req.end):
                continue
            blocked_by_existing = False
            for old_req in existing_reqs:
                old_equipment = existing_solution.get(old_req.item_id)
                if old_equipment == equipment_id and _overlap(
                    req.start, req.end, old_req.start, old_req.end
                ):
                    blocked_by_existing = True
                    break
            if not blocked_by_existing:
                options.append(equipment_id)
        if not options:
            return None
        candidates[req.item_id] = options

    assignment: dict[int, int] = {}

    def overlaps_new(a: Requirement, b: Requirement) -> bool:
        return a.item_id != b.item_id and _overlap(a.start, a.end, b.start, b.end)

    def dfs() -> bool:
        if len(assignment) == len(new_reqs):
            return True
        best: Requirement | None = None
        best_options: list[int] | None = None
        for req in new_reqs:
            if req.item_id in assignment:
                continue
            used = {
                assignment[other.item_id]
                for other in new_reqs
                if other.item_id in assignment and overlaps_new(req, other)
            }
            options = [eid for eid in candidates[req.item_id] if eid not in used]
            if not options:
                return False
            if best_options is None or len(options) < len(best_options):
                best, best_options = req, options
                if len(options) == 1:
                    break
        assert best is not None and best_options is not None
        for equipment_id in best_options:
            assignment[best.item_id] = equipment_id
            if dfs():
                return True
            del assignment[best.item_id]
        return False

    return dict(assignment) if dfs() else None


# --------------------------------------------------------------------- 状态推进
def _expire_due(conn, now: datetime, now_s: str) -> None:
    rows = conn.execute(
        "SELECT id, ends_at FROM reservations "
        "WHERE status IN ('waiting','confirmed') AND ends_at <= ?",
        (now_s,),
    ).fetchall()
    for row in rows:
        # 失效时点是约定结束时刻，而不是懒检查发生的时刻。
        conn.execute(
            "UPDATE reservations SET status='expired', expired_at=ends_at "
            "WHERE id=?",
            (row["id"],),
        )
        conn.execute(
            "DELETE FROM reservation_assignments WHERE reservation_id=?",
            (row["id"],),
        )


def _rebalance(conn, now: datetime, now_s: str) -> None:
    """按“先保留旧确认、再递补等候”的受理顺序重算全部未领取预约。"""
    _expire_due(conn, now, now_s)
    ctx = Availability.load(conn, now)
    orders = _load_active_orders(conn, now)

    confirmed = [o for o in orders.values() if o["status"] == "confirmed"]
    waiting = [o for o in orders.values() if o["status"] == "waiting"]
    confirmed.sort(key=lambda o: o["id"])
    waiting.sort(key=lambda o: o["id"])

    retained: list[dict[str, Any]] = []
    solution: dict[int, int] = {}

    def try_accept(order: dict[str, Any]) -> bool:
        # 快速路径：保留此前订单的现行派工，只给新单找机器。
        if solution:
            added = _extend_solution(order, retained, solution, ctx)
            if added is not None:
                solution.update(added)
                retained.append(order)
                return True
        # 精确路径：允许把所有未领取的已保留订单整体改派。
        next_solution = _solve_orders(retained + [order], ctx)
        if next_solution is None:
            return False
        solution.clear()
        solution.update(next_solution)
        retained.append(order)
        return True

    # 第一组：此前已确认的单，按最初受理顺序尽量保留；允许整体改派。
    for order in confirmed:
        if not try_accept(order):
            # 不能在不牺牲前面已保留单的情况下兑现 -> 整单降为等候并略过。
            conn.execute(
                "UPDATE reservations SET status='waiting', confirmed_at=NULL WHERE id=?",
                (order["id"],),
            )
            conn.execute(
                "DELETE FROM reservation_assignments WHERE reservation_id=?",
                (order["id"],),
            )
            order["status"] = "waiting"

    # 第二组：所有当前等候单（含刚刚被降级的旧确认）按最初受理顺序递补。
    waiting_rows = conn.execute(
        "SELECT * FROM reservations WHERE status='waiting' ORDER BY id"
    ).fetchall()
    for row in waiting_rows:
        order = orders.get(row["id"])
        if order is None:
            order = _load_order(conn, row, now)
            orders[row["id"]] = order
        order["status"] = "waiting"
        if not try_accept(order):
            continue
        conn.execute(
            "UPDATE reservations SET status='confirmed', confirmed_at=? WHERE id=?",
            (now_s, order["id"]),
        )

    # 方案不是锁：只持久化最近一次可兑现派工，供接口展示和交接时使用。
    conn.execute("DELETE FROM reservation_assignments")
    item_reservation = {
        req.item_id: req.reservation_id for req in _reqs_from_orders(retained)
    }
    for item_id, equipment_id in solution.items():
        reservation_id = item_reservation.get(item_id)
        if reservation_id is None:
            continue
        conn.execute(
            "INSERT INTO reservation_assignments"
            "(item_id, reservation_id, equipment_id, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (item_id, reservation_id, equipment_id, now_s),
        )


def _tick(conn, now: datetime | None = None) -> datetime:
    now = now or _now_dt()
    _rebalance(conn, now, _iso(now))
    return now


def refresh(db_path: str) -> None:
    """供应用启动/测试显式推进一次懒状态；所有查询接口内部也会调用。"""
    with tx(db_path) as conn:
        _tick(conn)


# --------------------------------------------------------------------- 输出组装
def _loan_out(row, now: datetime) -> dict[str, Any]:
    due = _dt(row["due_at"])
    returned_at = row["returned_at"]
    is_returned = returned_at is not None
    is_overdue = (not is_returned) and now >= due
    return {
        "id": row["id"],
        "reservation_id": row["reservation_id"],
        "reservation_item_id": row["reservation_item_id"],
        "equipment_id": row["equipment_id"],
        "asset_no": row["asset_no"] if "asset_no" in row.keys() else None,
        "equipment_name": row["equipment_name"] if "equipment_name" in row.keys() else None,
        "member_id": row["member_id"] if "member_id" in row.keys() else None,
        "member_code": row["member_code"] if "member_code" in row.keys() else None,
        "checked_out_at": row["checked_out_at"],
        "due_at": row["due_at"],
        "returned_at": returned_at,
        "status": "returned" if is_returned else "out",
        "is_returned": is_returned,
        "is_overdue": is_overdue,
    }


def _reservation_out(conn, row, now: datetime) -> dict[str, Any]:
    member = conn.execute(
        "SELECT code, name FROM members WHERE id=?", (row["member_id"],)
    ).fetchone()

    item_rows = conn.execute(
        "SELECT id, ordinal, label FROM reservation_items "
        "WHERE reservation_id=? ORDER BY ordinal, id",
        (row["id"],),
    ).fetchall()
    items: list[dict[str, Any]] = []
    loans: list[dict[str, Any]] = []
    active_loan_count = 0
    overdue_count = 0

    for item_row in item_rows:
        tags = [
            r["tag"]
            for r in conn.execute(
                "SELECT tag FROM reservation_item_tags WHERE item_id=? ORDER BY tag",
                (item_row["id"],),
            ).fetchall()
        ]
        assignment = conn.execute(
            "SELECT a.equipment_id, e.asset_no FROM reservation_assignments a "
            "JOIN equipment e ON e.id=a.equipment_id WHERE a.item_id=?",
            (item_row["id"],),
        ).fetchone()
        loan_row = conn.execute(
            "SELECT l.*, e.asset_no, e.name AS equipment_name, m.id AS member_id, "
            "m.code AS member_code FROM equipment_loans l "
            "JOIN equipment e ON e.id=l.equipment_id "
            "JOIN reservations r ON r.id=l.reservation_id "
            "JOIN members m ON m.id=r.member_id "
            "WHERE l.reservation_item_id=?",
            (item_row["id"],),
        ).fetchone()
        loan = _loan_out(loan_row, now) if loan_row else None
        if loan:
            loans.append(loan)
            if loan["status"] == "out":
                active_loan_count += 1
                if loan["is_overdue"]:
                    overdue_count += 1

        items.append(
            {
                "item_id": item_row["id"],
                "ordinal": item_row["ordinal"],
                "label": item_row["label"],
                "tags": tags,
                "required_tags": tags,
                "planned_equipment_id": assignment["equipment_id"] if assignment else None,
                "planned_asset_no": assignment["asset_no"] if assignment else None,
                "loan": loan,
            }
        )

    start = _dt(row["starts_at"])
    end = _dt(row["ends_at"])
    in_handover_window = start <= now < end
    is_overdue = overdue_count > 0
    if row["status"] in TERMINAL_PICKUP_STATUSES and is_overdue:
        display_status = "overdue"
    else:
        display_status = row["status"]

    return {
        "id": row["id"],
        "member_id": row["member_id"],
        "member_code": member["code"] if member else None,
        "member_name": member["name"] if member else None,
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "start_at": row["starts_at"],
        "end_at": row["ends_at"],
        "start_time": row["starts_at"],
        "end_time": row["ends_at"],
        "status": row["status"],
        "status_label": STATUS_LABELS.get(display_status, row["status"]),
        "display_status": display_status,
        "state": display_status,
        "is_overdue": is_overdue,
        "overdue_count": overdue_count,
        "active_loan_count": active_loan_count,
        "all_returned": bool(loans) and active_loan_count == 0,
        "in_handover_window": in_handover_window,
        "can_cancel": row["status"] in ACTIVE_RESERVATION_STATUSES,
        "can_handover": row["status"] == "confirmed" and in_handover_window,
        "is_picked_up": row["status"] in TERMINAL_PICKUP_STATUSES,
        "picked_up_at": row["collected_at"],
        "items": items,
        "checklist": items,
        "loans": loans,
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "cancelled_at": row["cancelled_at"],
        "expired_at": row["expired_at"],
        "collected_at": row["collected_at"],
        "returned_at": row["returned_at"],
    }


def _get_reservation_row(conn, reservation_id: int):
    row = conn.execute(
        "SELECT * FROM reservations WHERE id=?", (reservation_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "预约不存在")
    return row


def get_reservation(
    db_path: str, reservation_id: int, member_id: int | None = None
) -> dict[str, Any]:
    with tx(db_path) as conn:
        now = _tick(conn)
        row = _get_reservation_row(conn, reservation_id)
        if member_id is not None and row["member_id"] != member_id:
            # 不向其他会员揭示预约是否存在。
            raise ServiceError(404, "预约不存在")
        return _reservation_out(conn, row, now)


def list_reservations(
    db_path: str,
    member_id: int | None = None,
    status: str | None = None,
    member_code: str | None = None,
) -> list[dict[str, Any]]:
    with tx(db_path) as conn:
        now = _tick(conn)
        sql = "SELECT r.* FROM reservations r JOIN members m ON m.id=r.member_id"
        clauses: list[str] = []
        params: list[Any] = []
        if member_id is not None:
            clauses.append("r.member_id=?")
            params.append(member_id)
        if member_code:
            clauses.append("m.code=?")
            params.append(member_code)
        if status:
            clauses.append("r.status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY r.id DESC"
        rows = conn.execute(sql, params).fetchall()
        return [_reservation_out(conn, row, now) for row in rows]


# --------------------------------------------------------------------- 机具登记
def create_equipment(
    db_path: str,
    asset_no: str,
    tags: Iterable[str] | None = None,
    name: str = "",
) -> dict[str, Any]:
    asset_no = _clean_asset_no(asset_no)
    clean_tags = _clean_tags(list(tags or []))
    name = (name or "").strip()[:128]
    now_s = _iso(_now_dt())
    with tx(db_path) as conn:
        try:
            cur = conn.execute(
                "INSERT INTO equipment(asset_no, name, created_at) VALUES (?, ?, ?)",
                (asset_no, name, now_s),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(409, f"机具独立编号已存在: {asset_no}") from exc
        equipment_id = cur.lastrowid
        for tag in clean_tags:
            conn.execute(
                "INSERT INTO equipment_tags(equipment_id, tag) VALUES (?, ?)",
                (equipment_id, tag),
            )
    return get_equipment(db_path, equipment_id)


def _equipment_out(conn, row, now: datetime) -> dict[str, Any]:
    tags = [
        r["tag"]
        for r in conn.execute(
            "SELECT tag FROM equipment_tags WHERE equipment_id=? ORDER BY tag",
            (row["id"],),
        ).fetchall()
    ]
    maintenance = [
        _maintenance_out(mr, now)
        for mr in conn.execute(
            "SELECT * FROM maintenance_windows WHERE equipment_id=? ORDER BY starts_at, id",
            (row["id"],),
        ).fetchall()
    ]
    loan_row = conn.execute(
        "SELECT l.*, m.code AS member_code, m.name AS member_name, r.id AS rid "
        "FROM equipment_loans l "
        "JOIN reservations r ON r.id=l.reservation_id "
        "JOIN members m ON m.id=r.member_id "
        "WHERE l.equipment_id=? AND l.returned_at IS NULL ORDER BY l.id LIMIT 1",
        (row["id"],),
    ).fetchone()
    current_loan = None
    state = "available"
    if loan_row:
        due = _dt(loan_row["due_at"])
        overdue = now >= due
        state = "overdue" if overdue else "on_loan"
        current_loan = {
            "loan_id": loan_row["id"],
            "reservation_id": loan_row["reservation_id"],
            "member_code": loan_row["member_code"],
            "member_name": loan_row["member_name"],
            "checked_out_at": loan_row["checked_out_at"],
            "due_at": loan_row["due_at"],
            "is_overdue": overdue,
        }
    active_maintenance_now = any(
        w["status"] == "active"
        and _dt(w["starts_at"]) <= now < _dt(w["ends_at"])
        for w in maintenance
    )
    if current_loan is None and active_maintenance_now:
        state = "maintenance"

    planned_rows = conn.execute(
        "SELECT a.reservation_id, a.item_id FROM reservation_assignments a "
        "WHERE a.equipment_id=? ORDER BY a.reservation_id",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "asset_no": row["asset_no"],
        "code": row["asset_no"],
        "serial_number": row["asset_no"],
        "name": row["name"],
        "tags": tags,
        "labels": tags,
        "state": state,
        "current_loan": current_loan,
        "planned_reservation_ids": [r["reservation_id"] for r in planned_rows],
        "maintenance": maintenance,
        "created_at": row["created_at"],
        "retired_at": row["retired_at"],
    }


def get_equipment(db_path: str, equipment_id: int) -> dict[str, Any]:
    with tx(db_path) as conn:
        now = _tick(conn)
        row = conn.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
        if row is None:
            raise ServiceError(404, "机具不存在")
        return _equipment_out(conn, row, now)


def get_equipment_by_asset_no(db_path: str, asset_no: str) -> dict[str, Any]:
    with tx(db_path) as conn:
        now = _tick(conn)
        row = conn.execute(
            "SELECT * FROM equipment WHERE asset_no=?", (_clean_asset_no(asset_no),)
        ).fetchone()
        if row is None:
            raise ServiceError(404, "机具编号不存在")
        return _equipment_out(conn, row, now)


def list_equipment(db_path: str) -> list[dict[str, Any]]:
    with tx(db_path) as conn:
        now = _tick(conn)
        rows = conn.execute("SELECT * FROM equipment ORDER BY asset_no, id").fetchall()
        return [_equipment_out(conn, row, now) for row in rows]


# --------------------------------------------------------------------- 维修时段
def _resolve_equipment_id(
    conn, equipment_id: int | None = None, asset_no: str | None = None
) -> int:
    if equipment_id is not None:
        row = conn.execute("SELECT id FROM equipment WHERE id=?", (equipment_id,)).fetchone()
        if row is None:
            raise ServiceError(404, "机具不存在")
        return row["id"]
    if asset_no:
        row = conn.execute(
            "SELECT id FROM equipment WHERE asset_no=?", (_clean_asset_no(asset_no),)
        ).fetchone()
        if row is None:
            raise ServiceError(404, "机具编号不存在")
        return row["id"]
    raise ServiceError(422, "必须提供 equipment_id 或 asset_no")


def _maintenance_out(row, now: datetime) -> dict[str, Any]:
    start = _dt(row["starts_at"])
    end = _dt(row["ends_at"])
    return {
        "id": row["id"],
        "equipment_id": row["equipment_id"],
        "asset_no": row["asset_no"] if "asset_no" in row.keys() else None,
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "reason": row["reason"],
        "status": row["status"],
        "active_now": row["status"] == "active" and start <= now < end,
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
    }


def add_maintenance(
    db_path: str,
    starts_at: Any,
    ends_at: Any,
    equipment_id: int | None = None,
    asset_no: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    start = _dt(starts_at)
    end = _dt(ends_at)
    if end <= start:
        raise ServiceError(422, "维修停用结束时间必须晚于开始时间")
    now_s = _iso(_now_dt())
    with tx(db_path) as conn:
        now = _tick(conn)
        eq_id = _resolve_equipment_id(conn, equipment_id, asset_no)
        cur = conn.execute(
            "INSERT INTO maintenance_windows"
            "(equipment_id, starts_at, ends_at, reason, status, created_at) "
            "VALUES (?, ?, ?, ?, 'active', ?)",
            (eq_id, _iso(start), _iso(end), (reason or "").strip()[:200], now_s),
        )
        window_id = cur.lastrowid
        _tick(conn, now)
        row = conn.execute(
            "SELECT w.*, e.asset_no FROM maintenance_windows w "
            "JOIN equipment e ON e.id=w.equipment_id WHERE w.id=?",
            (window_id,),
        ).fetchone()
        return _maintenance_out(row, now)


def revoke_maintenance(db_path: str, window_id: int) -> dict[str, Any]:
    with tx(db_path) as conn:
        now = _tick(conn)
        row = conn.execute(
            "SELECT w.*, e.asset_no FROM maintenance_windows w "
            "JOIN equipment e ON e.id=w.equipment_id WHERE w.id=?",
            (window_id,),
        ).fetchone()
        if row is None:
            raise ServiceError(404, "维修时段不存在")
        if row["status"] != "revoked":
            conn.execute(
                "UPDATE maintenance_windows SET status='revoked', revoked_at=? WHERE id=?",
                (_iso(now), window_id),
            )
        _tick(conn, now)
        row = conn.execute(
            "SELECT w.*, e.asset_no FROM maintenance_windows w "
            "JOIN equipment e ON e.id=w.equipment_id WHERE w.id=?",
            (window_id,),
        ).fetchone()
        return _maintenance_out(row, now)


def list_maintenance(db_path: str, active_only: bool = False) -> list[dict[str, Any]]:
    with tx(db_path) as conn:
        now = _tick(conn)
        sql = (
            "SELECT w.*, e.asset_no FROM maintenance_windows w "
            "JOIN equipment e ON e.id=w.equipment_id"
        )
        if active_only:
            sql += " WHERE w.status='active'"
        sql += " ORDER BY w.starts_at DESC, w.id DESC"
        return [_maintenance_out(r, now) for r in conn.execute(sql).fetchall()]


# --------------------------------------------------------------------- 预约受理
def create_reservation(
    db_path: str,
    member_id: int,
    starts_at: Any,
    ends_at: Any,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(items, list) or not items:
        raise ServiceError(422, "借用清单至少需要一项")
    if len(items) > 100:
        raise ServiceError(422, "一张预约单最多 100 项")

    cleaned_items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items, start=1):
        if isinstance(raw_item, str):
            label = ""
            tags = _clean_tags([raw_item])
        elif isinstance(raw_item, (list, tuple, set, frozenset)):
            label = ""
            tags = _clean_tags(list(raw_item))
        elif isinstance(raw_item, dict):
            label = str(raw_item.get("label") or raw_item.get("name") or "").strip()[:128]
            raw_tags = raw_item.get("tags")
            if raw_tags is None:
                raw_tags = raw_item.get("required_tags")
            if raw_tags is None:
                raw_tags = raw_item.get("capability_tags")
            if raw_tags is None:
                raw_tags = raw_item.get("capabilities")
            if raw_tags is None:
                raw_tags = raw_item.get("tag")
            if isinstance(raw_tags, str):
                raw_tags = [raw_tags]
            tags = _clean_tags(raw_tags or [])
        else:
            raise ServiceError(422, "清单项必须是对象或标签字符串")
        cleaned_items.append({"ordinal": index, "label": label, "tags": tags})

    with tx(db_path) as conn:
        now = _now_dt()
        start = _dt(starts_at)
        end = _dt(ends_at)
        if start < now:
            raise ServiceError(
                422,
                f"预约开始时间 {_iso(start)} 不得早于服务端当前时间 {_iso(now)}",
            )
        if end <= start:
            raise ServiceError(422, "借用结束时间必须晚于开始时间")

        member = conn.execute("SELECT id FROM members WHERE id=?", (member_id,)).fetchone()
        if member is None:
            raise ServiceError(401, "会员身份不存在或已失效")

        now_s = _iso(now)
        cur = conn.execute(
            "INSERT INTO reservations(member_id, starts_at, ends_at, status, created_at) "
            "VALUES (?, ?, ?, 'waiting', ?)",
            (member_id, _iso(start), _iso(end), now_s),
        )
        reservation_id = cur.lastrowid
        for item in cleaned_items:
            item_cur = conn.execute(
                "INSERT INTO reservation_items(reservation_id, ordinal, label) "
                "VALUES (?, ?, ?)",
                (reservation_id, item["ordinal"], item["label"]),
            )
            item_id = item_cur.lastrowid
            for tag in item["tags"]:
                conn.execute(
                    "INSERT INTO reservation_item_tags(item_id, tag) VALUES (?, ?)",
                    (item_id, tag),
                )

        # 新单按 id 排在等候组末尾；同事务立即重算，使响应直接是确认/等候。
        _tick(conn, now)
        row = _get_reservation_row(conn, reservation_id)
        return _reservation_out(conn, row, now)


def cancel_reservation(
    db_path: str, reservation_id: int, member_id: int
) -> dict[str, Any]:
    with tx(db_path) as conn:
        now = _tick(conn)
        row = _get_reservation_row(conn, reservation_id)
        if row["member_id"] != member_id:
            raise ServiceError(404, "预约不存在")

        # 重复取消是幂等的；已领取/已归还不能再走会员取消。
        if row["status"] == "cancelled":
            return _reservation_out(conn, row, now)
        if row["status"] in TERMINAL_PICKUP_STATUSES:
            raise ServiceError(409, "预约已领取实物，不能取消；请逐件归还")
        if row["status"] == "expired":
            raise ServiceError(409, "预约已因未领取且超过结束时间而失效")

        conn.execute(
            "UPDATE reservations SET status='cancelled', cancelled_at=? WHERE id=?",
            (_iso(now), reservation_id),
        )
        conn.execute(
            "DELETE FROM reservation_assignments WHERE reservation_id=?",
            (reservation_id,),
        )
        _tick(conn, now)
        row = _get_reservation_row(conn, reservation_id)
        return _reservation_out(conn, row, now)


# --------------------------------------------------------------------- 交接/归还
def _selected_equipment_map(
    conn,
    items: list[dict[str, Any]],
    equipment_ids: list[int] | None,
    asset_nos: list[str] | None,
) -> dict[int, int]:
    selected: list[int] = []
    if equipment_ids:
        selected.extend(int(x) for x in equipment_ids)
    if asset_nos:
        for asset_no in asset_nos:
            row = conn.execute(
                "SELECT id FROM equipment WHERE asset_no=?",
                (_clean_asset_no(asset_no),),
            ).fetchone()
            if row is None:
                raise ServiceError(404, f"实物编号不存在: {asset_no}")
            selected.append(row["id"])

    if len(selected) != len(items):
        raise ServiceError(
            422,
            f"交接实物数量({len(selected)})必须与清单数量({len(items)})一致，一次领齐",
        )
    if len(set(selected)) != len(selected):
        raise ServiceError(422, "同一台机具不能在同一套预约中充当两件")

    result: dict[int, int] = {}
    for item, equipment_id in zip(items, selected):
        eq = conn.execute("SELECT id, asset_no FROM equipment WHERE id=?", (equipment_id,)).fetchone()
        if eq is None:
            raise ServiceError(404, f"机具 id 不存在: {equipment_id}")
        result[item["item_id"]] = equipment_id
    return result


def handover(
    db_path: str,
    reservation_id: int,
    equipment_ids: list[int] | None = None,
    asset_nos: list[str] | None = None,
) -> dict[str, Any]:
    with tx(db_path) as conn:
        now = _tick(conn)
        row = _get_reservation_row(conn, reservation_id)

        # 并发/重试交接：第一次已领齐后，后续请求返回同一实物编号。
        if row["status"] in TERMINAL_PICKUP_STATUSES:
            return _reservation_out(conn, row, now)
        if row["status"] != "confirmed":
            raise ServiceError(
                409,
                f"预约当前为 {row['status']}，只有确认且在借用时段内的预约可以交接",
            )

        start = _dt(row["starts_at"])
        end = _dt(row["ends_at"])
        if now < start:
            raise ServiceError(409, f"尚未到借用开始时间: {row['starts_at']}")
        if now >= end:
            # 正常情况下 _tick 已将其置为 expired；这里保留并发边界守卫。
            raise ServiceError(409, "已超过预约借用结束时间，预约已失效")

        order = _load_order(conn, row, now)
        items = order["items"]
        ctx = Availability.load(conn, now)

        if equipment_ids is not None or asset_nos is not None:
            selected_map = _selected_equipment_map(
                conn, items, equipment_ids, asset_nos
            )
        else:
            assignment_rows = conn.execute(
                "SELECT item_id, equipment_id FROM reservation_assignments "
                "WHERE reservation_id=? ORDER BY item_id",
                (reservation_id,),
            ).fetchall()
            selected_map = {r["item_id"]: r["equipment_id"] for r in assignment_rows}
            if len(selected_map) != len(items):
                # 防御性兜底：重算一次当前全部确认单。
                solution = _solve_orders([order], ctx)
                if solution is None:
                    raise ServiceError(409, "当前已无法为该预约整套派工")
                selected_map = solution

        # 校验标签、同套唯一性以及每台能覆盖从交接时刻到结束的剩余借期。
        extra_blocks: dict[int, list[tuple[datetime, datetime]]] = {}
        for item in items:
            item_id = item["item_id"]
            if item_id not in selected_map:
                raise ServiceError(422, "交接选择未覆盖全部清单项")
            equipment_id = selected_map[item_id]
            if not ctx.available(
                equipment_id, item["tags"], now, end
            ):
                raise ServiceError(
                    409,
                    f"机具 {equipment_id} 不满足标签，或在剩余借期内存在占用/维修/逾期未还",
                )
            extra_blocks.setdefault(equipment_id, []).append((now, end))

        # 把本次所选实物视为即将成立的硬借约，再求解其他全部确认单。
        other_rows = conn.execute(
            "SELECT * FROM reservations WHERE status='confirmed' AND id<>? ORDER BY id",
            (reservation_id,),
        ).fetchall()
        other_orders = [_load_order(conn, r, now) for r in other_rows]
        others_solution = _solve_orders(other_orders, ctx, extra_blocks=extra_blocks)
        if others_solution is None:
            raise ServiceError(409, "所选机具会使其他已确认预约失去整套可兑现性")

        now_s = _iso(now)
        for item in items:
            equipment_id = selected_map[item["item_id"]]
            conn.execute(
                "INSERT INTO equipment_loans"
                "(reservation_id, reservation_item_id, equipment_id, "
                " checked_out_at, due_at, status) "
                "VALUES (?, ?, ?, ?, ?, 'out')",
                (reservation_id, item["item_id"], equipment_id, now_s, row["ends_at"]),
            )

        conn.execute(
            "UPDATE reservations SET status='collected', collected_at=? WHERE id=?",
            (now_s, reservation_id),
        )
        conn.execute(
            "DELETE FROM reservation_assignments WHERE reservation_id=?",
            (reservation_id,),
        )

        # 新硬借约已落库；统一再走一次保留/递补，其他确认单可在此改派。
        _tick(conn, now)
        row = _get_reservation_row(conn, reservation_id)
        return _reservation_out(conn, row, now)


def return_loan(
    db_path: str,
    loan_id: int | None = None,
    asset_no: str | None = None,
    equipment_id: int | None = None,
) -> dict[str, Any]:
    with tx(db_path) as conn:
        now = _tick(conn)
        select_from = (
            "SELECT l.*, e.asset_no, e.name AS equipment_name, m.id AS member_id, "
            "m.code AS member_code FROM equipment_loans l "
            "JOIN equipment e ON e.id=l.equipment_id "
            "JOIN reservations r ON r.id=l.reservation_id "
            "JOIN members m ON m.id=r.member_id "
        )
        if loan_id is not None:
            row = conn.execute(
                select_from + "WHERE l.id=?",
                (loan_id,),
            ).fetchone()
            if row is None:
                raise ServiceError(404, "借还记录不存在")
        elif equipment_id is not None:
            row = conn.execute(
                select_from
                + "WHERE l.equipment_id=? AND l.returned_at IS NULL ORDER BY l.id LIMIT 1",
                (equipment_id,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    select_from
                    + "WHERE l.equipment_id=? ORDER BY l.id DESC LIMIT 1",
                    (equipment_id,),
                ).fetchone()
            if row is None:
                raise ServiceError(404, "该机具没有借还记录")
        elif asset_no:
            asset_no = _clean_asset_no(asset_no)
            row = conn.execute(
                select_from
                + "WHERE e.asset_no=? AND l.returned_at IS NULL ORDER BY l.id LIMIT 1",
                (asset_no,),
            ).fetchone()
            if row is None:
                # 幂等：若该编号最近一条已还，直接返回它，方便重试。
                row = conn.execute(
                    select_from + "WHERE e.asset_no=? ORDER BY l.id DESC LIMIT 1",
                    (asset_no,),
                ).fetchone()
                if row is None:
                    raise ServiceError(404, f"找不到机具借还记录: {asset_no}")
        else:
            raise ServiceError(422, "必须提供 loan_id、equipment_id 或 asset_no")

        if row["returned_at"] is None:
            conn.execute(
                "UPDATE equipment_loans SET returned_at=?, status='returned' WHERE id=?",
                (_iso(now), row["id"]),
            )

        # 若该单全部实物均已归还，推进预约终态。
        reservation_id = row["reservation_id"]
        open_count = conn.execute(
            "SELECT COUNT(*) AS n FROM equipment_loans "
            "WHERE reservation_id=? AND returned_at IS NULL",
            (reservation_id,),
        ).fetchone()["n"]
        if open_count == 0:
            reservation = conn.execute(
                "SELECT returned_at FROM reservations WHERE id=?",
                (reservation_id,),
            ).fetchone()
            # 幂等重试不得把真正的完结时间覆盖成重试时间。
            finished_at = (reservation["returned_at"] if reservation else None) or _iso(now)
            conn.execute(
                "UPDATE reservations SET status='returned', returned_at=? WHERE id=?",
                (finished_at, reservation_id),
            )

        # 可用性增加后：先保留确认，再按受理顺序递补等候。
        _tick(conn, now)

        updated = conn.execute(
            "SELECT l.*, e.asset_no, e.name AS equipment_name, m.id AS member_id, "
            "m.code AS member_code FROM equipment_loans l "
            "JOIN equipment e ON e.id=l.equipment_id "
            "JOIN reservations r ON r.id=l.reservation_id "
            "JOIN members m ON m.id=r.member_id WHERE l.id=?",
            (row["id"],),
        ).fetchone()
        return _loan_out(updated, now)


def list_loans(
    db_path: str,
    open_only: bool = False,
    returned_only: bool = False,
    overdue_only: bool = False,
    member_id: int | None = None,
) -> list[dict[str, Any]]:
    with tx(db_path) as conn:
        now = _tick(conn)
        sql = (
            "SELECT l.*, e.asset_no, e.name AS equipment_name, m.id AS member_id, "
            "m.code AS member_code FROM equipment_loans l "
            "JOIN equipment e ON e.id=l.equipment_id "
            "JOIN reservations r ON r.id=l.reservation_id "
            "JOIN members m ON m.id=r.member_id WHERE 1=1"
        )
        params: list[Any] = []
        if open_only:
            sql += " AND l.returned_at IS NULL"
        if returned_only:
            sql += " AND l.returned_at IS NOT NULL"
        if member_id is not None:
            sql += " AND r.member_id=?"
            params.append(member_id)
        sql += " ORDER BY l.id DESC"
        rows = conn.execute(sql, params).fetchall()
        result = [_loan_out(r, now) for r in rows]
        if overdue_only:
            result = [loan for loan in result if loan["is_overdue"]]
        return result
