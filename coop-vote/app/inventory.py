"""批次入库、拆并、备货、交货、取消与质量召回的本地持久化服务。

所有写操作均通过 :func:`app.database.tx` 使用进程内全局写锁和
``BEGIN IMMEDIATE`` 串行提交。拆并、备货、质量事件触发的整单退回以及
交货扣减都在单个事务内完成，因此不会出现半单占用、半单释放或半单出库。

数量模型
--------
``inv_batches.case_count`` 是仍在合作社库内的箱数（包含已备货占用）；
已交货箱数在同一事务内从 ``case_count`` 扣减并记入 ``delivered_cases``。
已备货占用通过 ``inv_outbound_allocations`` 与 ``status='prepared'``
的出库单计算，不另存可能不一致的占用列。
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Iterable

from .database import read, tx
from .service import ServiceError, utc_now as service_utc_now


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def utc_now() -> str:
    """可被测试 monkeypatch 的时钟入口。"""
    return service_utc_now()


def _first(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def clean_text(value: Any, field: str, *, required: bool = False, max_length: int = 128) -> str:
    if value is None:
        text = ""
    else:
        text = str(value).strip()
    if required and not text:
        raise ServiceError(422, f"{field}不能为空")
    if len(text) > max_length:
        raise ServiceError(422, f"{field}最长 {max_length} 个字符")
    return text


def positive_int(value: Any, field: str = "箱数") -> int:
    if isinstance(value, bool):
        raise ServiceError(422, f"{field}必须是正整数")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text or not (text.lstrip("+").isdigit()):
            raise ServiceError(422, f"{field}必须是正整数")
        number = int(text)
    else:
        raise ServiceError(422, f"{field}必须是正整数")
    if number <= 0:
        raise ServiceError(422, f"{field}必须是正整数")
    return number


def _now() -> str:
    return utc_now()


# ---------------------------------------------------------------------------
# 请求别名归一化
# ---------------------------------------------------------------------------
BATCH_NO_KEYS = (
    "batch_no", "batch_number", "batch_code", "batch",
    "lot_no", "lot_number", "lot_code", "lot",
    "code", "number", "no",
)
BATCH_ID_KEYS = ("batch_id", "lot_id", "id")
CASE_KEYS = (
    "cases", "case_count", "total_cases", "box_count", "boxes",
    "quantity", "qty", "amount", "count", "case_qty", "box_qty",
    "case_quantity", "box_quantity", "number_of_cases", "number_of_boxes",
    "take_cases", "split_cases", "used_cases", "take", "used",
    "source_cases", "split_quantity", "take_quantity",
)
ALLOCATION_LIST_KEYS = (
    "allocations", "batch_allocations", "items", "lines", "picks",
    "details", "sources", "source_batches", "splits", "distribution",
)
SOURCE_LIST_KEYS = (
    "sources", "source_batches", "inputs", "input_batches", "from_batches",
    "allocations", "items", "lines", "splits", "parts",
)


def _batch_ref_from_dict(data: dict[str, Any]) -> dict[str, Any]:
    generic_ref = _first(data, "batch_ref", "lot_ref", "source_ref", "ref")
    if isinstance(generic_ref, dict):
        result = _batch_ref_from_dict(generic_ref)
        qty = _first(data, *CASE_KEYS)
        if qty is not None and "cases" not in result:
            result["cases"] = qty
        return result
    if generic_ref is not None:
        result = {"batch_ref": generic_ref}
        qty = _first(data, *CASE_KEYS)
        if qty is not None:
            result["cases"] = qty
        return result

    nested = _first(data, "batch", "lot", "source_batch")
    if isinstance(nested, (str, int)) and not isinstance(nested, bool):
        return {"batch_ref": nested}
    if isinstance(nested, dict):
        result = _batch_ref_from_dict(nested)
        qty = _first(data, *CASE_KEYS)
        if qty is not None:
            result["cases"] = qty
        return result

    batch_id = _first(data, "batch_id", "lot_id")
    batch_no = _first(data, *BATCH_NO_KEYS)
    # “id”只有在没有明显出库单/事件 id 语义干扰时才作为批次 id。
    if batch_id is None and "batch_id" not in data and "lot_id" not in data:
        batch_id = data.get("id") if _first(data, *BATCH_NO_KEYS[:-2]) is None else None
    cases = _first(data, *CASE_KEYS)
    out: dict[str, Any] = {}
    if batch_id is not None:
        out["batch_id"] = batch_id
    if batch_no is not None:
        out["batch_no"] = batch_no
    if not out and data.get("id") is not None:
        out["batch_id"] = data["id"]
    if cases is not None:
        out["cases"] = cases
    return out


def normalize_specs(raw: Any, list_keys: Iterable[str] = ALLOCATION_LIST_KEYS) -> list[dict[str, Any]]:
    """把多种常见请求写法归一为 ``[{batch_id|batch_no|batch_ref, cases}]``。"""
    if raw is None:
        return []

    if isinstance(raw, dict):
        # 形如 {"B-1": 3, "B-2": 5} 的映射。
        looks_like_item = any(k in raw for k in ("batch_id", "lot_id")) or any(
            k in raw for k in BATCH_NO_KEYS
        )
        qty = _first(raw, *CASE_KEYS)
        if looks_like_item and qty is not None:
            raw = [raw]
        else:
            raw = [{"batch_no": key, "cases": value} for key, value in raw.items()]

    if isinstance(raw, (str, int)) and not isinstance(raw, bool):
        return [{"batch_ref": raw}]

    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise ServiceError(422, "批次分配必须是数组或批次号→箱数的对象")

    specs: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if isinstance(item, (tuple, list)) and len(item) == 2 and not isinstance(item[0], (dict, list)):
            specs.append({"batch_ref": item[0], "cases": item[1], "ordinal": index})
        elif isinstance(item, (str, int)) and not isinstance(item, bool):
            # 允许 ["B1", "B2"] 与并行箱数字段配合；否则缺少数量会在后面报错。
            specs.append({"batch_ref": item})
        elif isinstance(item, dict):
            spec = _batch_ref_from_dict(item)
            if "cases" not in spec:
                qty = _first(item, *CASE_KEYS)
                if qty is not None:
                    spec["cases"] = qty
            spec["ordinal"] = index
            specs.append(spec)
        else:
            raise ServiceError(422, "批次分配项必须是对象或批次号")
    return specs


def specs_from_body(body: dict[str, Any], *, source: bool = False) -> list[dict[str, Any]]:
    keys = SOURCE_LIST_KEYS if source else ALLOCATION_LIST_KEYS
    raw = _first(body, *keys)
    specs = normalize_specs(raw, keys)

    # 并行数组：batch_nos=["A","B"], cases=[2,3] / quantities=[...]
    if not specs:
        refs = _first(
            body,
            "batch_nos", "batch_numbers", "batch_ids", "lots", "lot_nos",
            "source_batch_nos", "source_batch_ids",
        )
        quantities = _first(body, "cases_list", "quantities", "case_counts", "boxes_list")
        if isinstance(refs, list):
            qty_list = quantities if isinstance(quantities, list) else []
            for i, ref in enumerate(refs):
                spec: dict[str, Any] = {"batch_ref": ref}
                if i < len(qty_list):
                    spec["cases"] = qty_list[i]
                specs.append(spec)

    # 单个批次的便捷写法：/admin/batches/{id}/split + {"cases": 3, ...}
    if not specs:
        ref = _first(body, "source_batch_no", "source_batch_id", "from_batch", "batch_ref")
        cases = _first(body, *CASE_KEYS)
        if ref is not None and cases is not None:
            specs = [{"batch_ref": ref, "cases": cases}]
        elif _first(body, *BATCH_NO_KEYS, "batch_id", "lot_id") is not None and cases is not None:
            specs = [_batch_ref_from_dict(body)]
    return specs


def buyer_from_body(body: dict[str, Any]) -> tuple[str, str]:
    buyer = _first(body, "buyer", "buyer_name", "customer", "customer_name",
                   "recipient", "receiver", "buyer_info")
    buyer_code = _first(body, "buyer_code", "customer_code", "member_code", "buyer_id")
    if isinstance(buyer, dict):
        name = _first(buyer, "name", "buyer_name", "full_name", "code")
        code = _first(buyer, "code", "buyer_code", "id", "member_code")
        buyer = name
        if buyer_code is None:
            buyer_code = code
    buyer_name = clean_text(buyer or "", "买家", required=False, max_length=128)
    return buyer_name, clean_text(buyer_code or "", "买家编号", max_length=64)


def _resolve_buyer_reference(db_path: str, body: dict[str, Any],
                             buyer_name: str, buyer_code: str) -> tuple[str, str]:
    """允许只传本地 members 的 code/id 作为买家；查不到时保留原始文本。"""
    if buyer_name:
        return buyer_name, buyer_code
    ref = _first(body, "buyer_id", "member_id", "buyer_code", "member_code",
                 "customer_code", "buyer")
    if ref is None:
        raise ServiceError(422, "买家不能为空")
    with read(db_path) as conn:
        row = None
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            row = conn.execute("SELECT code,name FROM members WHERE id=?", (int(ref),)).fetchone()
        if row is None:
            row = conn.execute("SELECT code,name FROM members WHERE code=?", (str(ref),)).fetchone()
        if row is not None:
            return row["name"], row["code"]
    text = clean_text(ref, "买家", required=True, max_length=128)
    return text, buyer_code


def variety_from_body(body: dict[str, Any], *, required: bool = True) -> str:
    value = _first(body, "variety", "product_variety", "cultivar", "produce_variety",
                   "crop", "crop_variety", "product", "produce_name", "species",
                   "variety_name")
    # “name”过于通用，只在明显是品种请求时由调用方另行处理。
    return clean_text(value, "品种", required=required, max_length=128)


def note_from_body(body: dict[str, Any]) -> str:
    value = _first(body, "note", "notes", "remark", "remarks", "comment", "comments")
    return clean_text(value or "", "备注", max_length=500)


# ---------------------------------------------------------------------------
# 行装载与响应
# ---------------------------------------------------------------------------
def _get_batch_row(conn: sqlite3.Connection, batch_ref: Any) -> sqlite3.Row:
    if batch_ref is None:
        raise ServiceError(422, "必须指定批次")
    # 明确是整数/数字时先按主键查；字符串则优先按业务编号查，再兼容数字 id。
    if isinstance(batch_ref, int) and not isinstance(batch_ref, bool):
        row = conn.execute("SELECT * FROM inv_batches WHERE id=?", (batch_ref,)).fetchone()
        if row is not None:
            return row
    text = str(batch_ref).strip()
    row = conn.execute("SELECT * FROM inv_batches WHERE batch_no=?", (text,)).fetchone()
    if row is not None:
        return row
    if text.isdigit():
        row = conn.execute("SELECT * FROM inv_batches WHERE id=?", (int(text),)).fetchone()
        if row is not None:
            return row
    raise ServiceError(404, f"批次不存在：{batch_ref}")


def _resolve_spec_batch(conn: sqlite3.Connection, spec: dict[str, Any]) -> sqlite3.Row:
    if spec.get("batch_id") is not None:
        try:
            batch_id = int(spec["batch_id"])
        except (TypeError, ValueError) as exc:
            raise ServiceError(422, "batch_id 必须是整数") from exc
        row = conn.execute("SELECT * FROM inv_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise ServiceError(404, f"批次不存在：id={batch_id}")
        return row
    if spec.get("batch_no") is not None:
        return _get_batch_row(conn, spec["batch_no"])
    if spec.get("batch_ref") is not None:
        return _get_batch_row(conn, spec["batch_ref"])
    raise ServiceError(422, "分配项缺少 batch_id 或 batch_no")


def _get_order_row(conn: sqlite3.Connection, order_ref: Any) -> sqlite3.Row:
    if order_ref is None:
        raise ServiceError(422, "必须指定出库单")
    if isinstance(order_ref, int) and not isinstance(order_ref, bool):
        row = conn.execute("SELECT * FROM inv_outbound_orders WHERE id=?", (order_ref,)).fetchone()
        if row is not None:
            return row
    text = str(order_ref).strip()
    row = conn.execute("SELECT * FROM inv_outbound_orders WHERE order_no=?", (text,)).fetchone()
    if row is not None:
        return row
    if text.isdigit():
        row = conn.execute("SELECT * FROM inv_outbound_orders WHERE id=?", (int(text),)).fetchone()
        if row is not None:
            return row
    raise ServiceError(404, f"出库单不存在：{order_ref}")


def _get_event_row(conn: sqlite3.Connection, event_ref: Any) -> sqlite3.Row:
    if event_ref is None:
        raise ServiceError(422, "必须指定质量事件")
    if isinstance(event_ref, int) and not isinstance(event_ref, bool):
        row = conn.execute("SELECT * FROM inv_quality_events WHERE id=?", (event_ref,)).fetchone()
        if row is not None:
            return row
    text = str(event_ref).strip()
    row = conn.execute("SELECT * FROM inv_quality_events WHERE event_no=?", (text,)).fetchone()
    if row is not None:
        return row
    if text.isdigit():
        row = conn.execute("SELECT * FROM inv_quality_events WHERE id=?", (int(text),)).fetchone()
        if row is not None:
            return row
    raise ServiceError(404, f"质量事件不存在：{event_ref}")


def _active_event_rows(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT DISTINCT e.*
        FROM inv_quality_events e
        JOIN inv_batch_sources s
          ON s.source_batch_id = e.target_batch_id
        WHERE s.batch_id=? AND e.status='active'
        ORDER BY e.id
        """,
        (batch_id,),
    ).fetchall()


def _reserved_cases(conn: sqlite3.Connection, batch_id: int,
                    exclude_order_id: int | None = None) -> int:
    sql = """
        SELECT COALESCE(SUM(a.cases), 0) AS qty
        FROM inv_outbound_allocations a
        JOIN inv_outbound_orders o ON o.id = a.order_id
        WHERE a.batch_id=? AND o.status='prepared'
    """
    params: list[Any] = [batch_id]
    if exclude_order_id is not None:
        sql += " AND a.order_id <> ?"
        params.append(exclude_order_id)
    return int(conn.execute(sql, params).fetchone()["qty"])


def _event_brief(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event_no": row["event_no"],
        "status": row["status"],
        "title": row["title"],
        "reason": row["reason"],
        "severity": row["severity"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
    }


def _batch_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    bid = row["id"]
    reserved = _reserved_cases(conn, bid)
    events = [_event_brief(r) for r in _active_event_rows(conn, bid)]
    available = int(row["case_count"]) - reserved

    direct_sources = [
        {
            "source_batch_id": r["source_batch_id"],
            "batch_id": r["source_batch_id"],
            "batch_no": r["batch_no"],
            "variety": r["variety"],
            "cases": int(r["cases"]),
        }
        for r in conn.execute(
            """
            SELECT ci.source_batch_id, ci.cases, b.batch_no, b.variety
            FROM inv_composition_items ci
            JOIN inv_compositions c ON c.id = ci.composition_id
            JOIN inv_batches b ON b.id = ci.source_batch_id
            WHERE c.new_batch_id=?
            ORDER BY ci.id
            """,
            (bid,),
        ).fetchall()
    ]
    all_sources = [
        {
            "source_batch_id": r["source_batch_id"],
            "batch_id": r["source_batch_id"],
            "batch_no": r["batch_no"],
        }
        for r in conn.execute(
            """
            SELECT bs.source_batch_id, b.batch_no
            FROM inv_batch_sources bs
            JOIN inv_batches b ON b.id = bs.source_batch_id
            WHERE bs.batch_id=? AND bs.source_batch_id <> ?
            ORDER BY bs.source_batch_id
            """,
            (bid, bid),
        ).fetchall()
    ]

    data = dict(row)
    stock_status = (
        "quarantined" if events
        else "depleted" if int(row["case_count"]) == 0
        else "reserved" if available == 0
        else "available"
    )
    data.update(
        {
            "batch_id": row["id"],
            "status": "quarantined" if events else "active",
            "state": "quarantined" if events else "active",
            "stock_status": stock_status,
            "availability_status": stock_status,
            "cases": row["case_count"],
            "case_count": int(row["case_count"]),
            "current_cases": int(row["case_count"]),
            "remaining_cases": int(row["case_count"]),
            "remaining_boxes": int(row["case_count"]),
            "box_count": int(row["case_count"]),
            "boxes": int(row["case_count"]),
            "quantity": int(row["case_count"]),
            "qty": int(row["case_count"]),
            "stock_cases": int(row["case_count"]),
            "on_hand_cases": int(row["case_count"]),
            "total_cases": int(row["case_count"]),
            "initial_cases": int(row["initial_cases"]),
            "delivered_cases": int(row["delivered_cases"]),
            "reserved_cases": reserved,
            "occupied_cases": reserved,
            "reserved_boxes": reserved,
            "occupied_boxes": reserved,
            "allocated_reserved_cases": reserved,
            "available_cases": available,
            "unoccupied_cases": available,
            "unreserved_cases": available,
            "free_cases": available,
            "available_boxes": available,
            "available": available,
            "quarantined": bool(events),
            "is_quarantined": bool(events),
            "active_event_ids": [e["id"] for e in events],
            "active_events": events,
            "direct_sources": direct_sources,
            "sources": direct_sources,
            "all_sources": all_sources,
            "source_batch_ids": [s["source_batch_id"] for s in all_sources],
            "source_batch_nos": [s["batch_no"] for s in all_sources],
        }
    )
    return data


def _allocation_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    batch = conn.execute(
        "SELECT batch_no, variety, case_count FROM inv_batches WHERE id=?",
        (row["batch_id"],),
    ).fetchone()
    data = dict(row)
    cases = int(row["cases"])
    data.update(
        {
            "cases": cases,
            "case_count": cases,
            "box_count": cases,
            "boxes": cases,
            "quantity": cases,
            "qty": cases,
            "batch_number": row["batch_no_snapshot"],
            "batch_no": row["batch_no_snapshot"],
            "variety": row["variety_snapshot"],
            "current_batch_no": batch["batch_no"] if batch else row["batch_no_snapshot"],
        }
    )
    return data


def _delivery_out(conn: sqlite3.Connection, order_id: int) -> dict[str, Any] | None:
    delivery = conn.execute(
        "SELECT * FROM inv_deliveries WHERE order_id=?", (order_id,)
    ).fetchone()
    if delivery is None:
        return None
    items = [
        {
            **dict(r),
            "cases": int(r["cases"]),
            "case_count": int(r["cases"]),
            "quantity": int(r["cases"]),
            "qty": int(r["cases"]),
            "batch_number": r["batch_no"],
        }
        for r in conn.execute(
            "SELECT * FROM inv_delivery_items WHERE delivery_id=? ORDER BY id",
            (delivery["id"],),
        ).fetchall()
    ]
    data = dict(delivery)
    data["items"] = items
    data["allocations"] = items
    data["total_cases"] = sum(i["cases"] for i in items)
    return data


ORDER_STATUS_LABELS = {
    "pending": "待备货",
    "prepared": "已备货",
    "delivered": "已交货",
    "cancelled": "已取消",
}


def _order_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    oid = row["id"]
    allocation_rows = conn.execute(
        "SELECT * FROM inv_outbound_allocations WHERE order_id=? ORDER BY id",
        (oid,),
    ).fetchall()
    allocations = [_allocation_out(conn, r) for r in allocation_rows]
    allocated = sum(a["cases"] for a in allocations)

    event_rows = conn.execute(
        """
        SELECT DISTINCT e.id, e.event_no, e.status, e.reason, e.title
        FROM inv_quality_events e
        JOIN inv_batch_sources s
          ON s.source_batch_id = e.target_batch_id
        JOIN inv_outbound_allocations a
          ON a.batch_id = s.batch_id
        WHERE a.order_id=? AND e.status='active'
        ORDER BY e.id
        """,
        (oid,),
    ).fetchall()
    blocking_events = [dict(r) for r in event_rows]

    can_prepare = row["status"] == "pending" and allocated == int(row["total_cases"])
    if can_prepare:
        for alloc in allocations:
            batch = conn.execute(
                "SELECT case_count FROM inv_batches WHERE id=?", (alloc["batch_id"],)
            ).fetchone()
            reserved_other = _reserved_cases(conn, alloc["batch_id"], exclude_order_id=oid)
            if batch is None or alloc["cases"] > int(batch["case_count"]) - reserved_other:
                can_prepare = False
                break
            if _active_event_rows(conn, alloc["batch_id"]):
                can_prepare = False
                break

    data = dict(row)
    total = int(row["total_cases"])
    data.update(
        {
            "order_id": oid,
            "buyer": row["buyer_name"],
            "customer": row["buyer_name"],
            "cases": total,
            "case_count": total,
            "box_count": total,
            "boxes": total,
            "quantity": total,
            "qty": total,
            "allocated_cases": allocated,
            "allocation_total": allocated,
            "remaining_cases": total - allocated,
            "unallocated_cases": total - allocated,
            "is_complete": allocated == total,
            "complete": allocated == total,
            "allocations": allocations,
            "items": allocations,
            "lines": allocations,
            "batches": allocations,
            "delivery": _delivery_out(conn, oid),
            "blocking_events": blocking_events,
            "active_event_ids": [e["id"] for e in blocking_events],
            "quarantined": bool(blocking_events),
            "status_label": ORDER_STATUS_LABELS.get(row["status"], row["status"]),
            "state": row["status"],
            "allocation_status": (
                "allocated" if row["status"] in ("prepared", "delivered")
                else "cancelled" if row["status"] == "cancelled"
                else "pending"
            ),
            "fulfillment_status": (
                "shipped" if row["status"] == "delivered"
                else "cancelled" if row["status"] == "cancelled"
                else "ready" if row["status"] == "prepared"
                else "pending"
            ),
            "can_prepare": can_prepare,
            "can_deliver": row["status"] == "prepared" and not blocking_events,
            "can_cancel": row["status"] in ("pending", "prepared"),
            "is_delivered": row["status"] == "delivered",
            "is_cancelled": row["status"] == "cancelled",
            "is_prepared": row["status"] == "prepared",
        }
    )
    return data


def _recall_item_out(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    cases = int(row["cases"])
    data.update(
        {
            "cases": cases,
            "case_count": cases,
            "quantity": cases,
            "qty": cases,
            "batch_number": row["batch_no"],
        }
    )
    return data


def _recall_out(conn: sqlite3.Connection, row: sqlite3.Row,
                include_order: bool = True) -> dict[str, Any]:
    items = [
        _recall_item_out(r)
        for r in conn.execute(
            "SELECT * FROM inv_recall_items WHERE case_id=? ORDER BY id",
            (row["id"],),
        ).fetchall()
    ]
    event = _get_event_row(conn, row["event_id"])
    data = dict(row)
    data["recall_id"] = row["id"]
    data["buyer"] = row["buyer_name"]
    data["customer"] = row["buyer_name"]
    data["outbound_order_id"] = row["order_id"]
    data["contacted"] = bool(row["contacted"])
    data["is_contacted"] = bool(row["contacted"])
    data["contact_status"] = "contacted" if row["contacted"] else "pending"
    data["items"] = items
    data["affected_batches"] = items
    data["event"] = _event_brief(event)
    data["quality_event"] = _event_brief(event)
    data["total_affected_cases"] = sum(i["cases"] for i in items)
    if include_order:
        order = conn.execute(
            "SELECT * FROM inv_outbound_orders WHERE id=?", (row["order_id"],)
        ).fetchone()
        if order is not None:
            data["order"] = _order_out(conn, order)
            data["outbound_order"] = _order_out(conn, order)
    return data


def _event_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    target = _get_batch_row(conn, row["target_batch_id"])
    affected_rows = conn.execute(
        """
        SELECT b.*
        FROM inv_batch_sources s
        JOIN inv_batches b ON b.id = s.batch_id
        WHERE s.source_batch_id=?
        ORDER BY b.id
        """,
        (row["target_batch_id"],),
    ).fetchall()
    affected_batches = [_batch_out(conn, r) for r in affected_rows]

    recall_rows = conn.execute(
        "SELECT * FROM inv_recall_cases WHERE event_id=? ORDER BY id",
        (row["id"],),
    ).fetchall()
    recalls = [_recall_out(conn, r, include_order=True) for r in recall_rows]

    action_rows = conn.execute(
        "SELECT * FROM inv_event_order_actions WHERE event_id=? ORDER BY id",
        (row["id"],),
    ).fetchall()
    released_order_ids = [int(r["order_id"]) for r in action_rows]
    released_orders = []
    for oid in released_order_ids:
        order_row = conn.execute(
            "SELECT * FROM inv_outbound_orders WHERE id=?", (oid,)
        ).fetchone()
        if order_row is not None:
            released_orders.append(_order_out(conn, order_row))

    data = dict(row)
    data.update(
        {
            "event_id": row["id"],
            "state": "open" if row["status"] == "active" else "resolved",
            "event_status": "open" if row["status"] == "active" else "resolved",
            "target_batch": _batch_out(conn, target),
            "target_batch_no": target["batch_no"],
            "affected_batches": affected_batches,
            "quarantined_batches": affected_batches,
            "affected_batch_ids": [b["id"] for b in affected_batches],
            "affected_batch_nos": [b["batch_no"] for b in affected_batches],
            "recalls": recalls,
            "recall_cases": recalls,
            "affected_orders": recalls,
            "released_order_ids": released_order_ids,
            "returned_order_ids": released_order_ids,
            "released_orders": released_orders,
            "returned_orders": released_orders,
            "rolled_back_orders": released_orders,
            "prepared_orders_returned_to_pending": released_orders,
            "is_resolved": row["status"] == "resolved",
            "is_active": row["status"] == "active",
            "quarantined": row["status"] == "active",
            "quarantine_active": row["status"] == "active",
            "recall_count": len(recalls),
            "affected_buyer_count": len({r["buyer_name"] for r in recalls}),
        }
    )
    return data


# ---------------------------------------------------------------------------
# 入库与查询
# ---------------------------------------------------------------------------
def create_batch(
    db_path: str,
    batch_no: str | None,
    variety: str,
    cases: Any,
    *,
    note: str = "",
    origin: str = "inbound",
) -> dict[str, Any]:
    variety = clean_text(variety, "品种", required=True)
    qty = positive_int(cases, "箱数")
    provided_no = clean_text(batch_no, "批次号", max_length=64) if batch_no is not None else ""
    now = _now()

    with tx(db_path) as conn:
        if provided_no:
            existing = conn.execute(
                "SELECT id FROM inv_batches WHERE batch_no=?", (provided_no,)
            ).fetchone()
            if existing is not None:
                raise ServiceError(409, f"批次号已存在：{provided_no}")
            insert_no = provided_no
        else:
            insert_no = f"TMP-{uuid.uuid4().hex}"

        try:
            cur = conn.execute(
                """
                INSERT INTO inv_batches(
                    batch_no, variety, origin, initial_cases, case_count,
                    delivered_cases, note, created_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (insert_no, variety, origin, qty, qty, note, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(409, f"批次号已存在：{provided_no or insert_no}") from exc
        bid = int(cur.lastrowid)

        if not provided_no:
            final_no = f"B-{bid:06d}"
            conn.execute("UPDATE inv_batches SET batch_no=? WHERE id=?", (final_no, bid))
            insert_no = final_no

        conn.execute(
            "INSERT INTO inv_batch_sources(batch_id, source_batch_id) VALUES (?, ?)",
            (bid, bid),
        )
        row = conn.execute("SELECT * FROM inv_batches WHERE id=?", (bid,)).fetchone()
        return _batch_out(conn, row)


def create_batch_from_payload(db_path: str, body: dict[str, Any]) -> dict[str, Any]:
    batch_no = _first(body, *BATCH_NO_KEYS)
    variety = variety_from_body(body)
    cases = _first(body, *CASE_KEYS)
    if cases is None:
        raise ServiceError(422, "必须提供正整数箱数 cases")
    note = note_from_body(body)
    return create_batch(db_path, batch_no, variety, cases, note=note)


def get_batch(db_path: str, batch_ref: Any) -> dict[str, Any]:
    with read(db_path) as conn:
        row = _get_batch_row(conn, batch_ref)
        return _batch_out(conn, row)


def list_batches(
    db_path: str,
    *,
    variety: str | None = None,
    quarantined: bool | None = None,
    available_only: bool = False,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM inv_batches WHERE 1=1"
    params: list[Any] = []
    if variety:
        sql += " AND variety=?"
        params.append(str(variety).strip())
    sql += " ORDER BY id"
    with read(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        result = [_batch_out(conn, r) for r in rows]
    if quarantined is not None:
        result = [b for b in result if b["quarantined"] is quarantined]
    if available_only:
        result = [b for b in result if b["available_cases"] > 0]
    return result


# ---------------------------------------------------------------------------
# 拆并
# ---------------------------------------------------------------------------
def _resolve_source_specs(conn: sqlite3.Connection, raw_specs: list[dict[str, Any]],
                          expected_variety: str | None = None) -> list[dict[str, Any]]:
    if not raw_specs:
        raise ServiceError(422, "必须提供至少一个来源批次及拆出箱数")

    aggregated: dict[int, dict[str, Any]] = {}
    variety: str | None = expected_variety
    for raw in raw_specs:
        spec = dict(raw)
        if "cases" not in spec or spec.get("cases") is None:
            raise ServiceError(422, "每个来源批次都必须指定拆出箱数")
        qty = positive_int(spec["cases"], "拆出箱数")
        row = _resolve_spec_batch(conn, spec)
        if variety is None:
            variety = row["variety"]
        elif row["variety"] != variety:
            raise ServiceError(409, "只能合并同一品种的批次")
        if expected_variety is not None and row["variety"] != expected_variety:
            raise ServiceError(422, "新品种与来源批次品种不一致")

        item = aggregated.setdefault(
            int(row["id"]),
            {"batch_id": row["id"], "batch_no": row["batch_no"],
             "variety": row["variety"], "cases": 0},
        )
        item["cases"] += qty

    return list(aggregated.values())


def compose_batches(
    db_path: str,
    sources: list[dict[str, Any]] | dict[str, Any] | str | None = None,
    new_batch_no: Any = None,
    variety: str | None = None,
    note: str = "",
    *legacy_args: Any,
    **legacy_kwargs: Any,
) -> dict[str, Any]:
    # 兼容部分直接服务层调用：compose(db, "NEW", [source, ...])。
    if isinstance(sources, str) and new_batch_no is not None and not isinstance(new_batch_no, str):
        sources, new_batch_no = new_batch_no, sources
    if legacy_args and sources is not None and new_batch_no is None:
        new_batch_no = legacy_args[0]
    new_batch_no = legacy_kwargs.get("new_batch_no", new_batch_no)
    variety = legacy_kwargs.get("variety", variety)
    note = legacy_kwargs.get("note", note)
    raw_specs = normalize_specs(sources if sources is not None else [])
    provided_no = clean_text(new_batch_no, "新批次号", max_length=64) if new_batch_no else ""
    expected_variety = clean_text(variety, "品种", required=False) if variety else None
    now = _now()

    with tx(db_path) as conn:
        specs = _resolve_source_specs(conn, raw_specs, expected_variety)
        total = sum(s["cases"] for s in specs)
        if total <= 0:
            raise ServiceError(422, "拆并总箱数必须为正整数")

        if provided_no:
            existing = conn.execute(
                "SELECT id FROM inv_batches WHERE batch_no=?", (provided_no,)
            ).fetchone()
            if existing is not None:
                raise ServiceError(409, f"新批次号已存在：{provided_no}")
            insert_no = provided_no
        else:
            insert_no = f"TMP-{uuid.uuid4().hex}"

        # 所有来源先做隔离与可用量校验；任一不满足则整笔失败。
        for spec in specs:
            bid = spec["batch_id"]
            events = _active_event_rows(conn, bid)
            if events:
                raise ServiceError(
                    409,
                    f"批次 {spec['batch_no']} 正在隔离中，不能拆并",
                )
            batch = conn.execute("SELECT * FROM inv_batches WHERE id=?", (bid,)).fetchone()
            reserved = _reserved_cases(conn, bid)
            available = int(batch["case_count"]) - reserved
            if spec["cases"] > available:
                raise ServiceError(
                    409,
                    f"批次 {spec['batch_no']} 可拆库存不足："
                    f"请求 {spec['cases']} 箱，未占用 {available} 箱",
                )

        output_variety = expected_variety or specs[0]["variety"]
        try:
            cur = conn.execute(
                """
                INSERT INTO inv_batches(
                    batch_no, variety, origin, initial_cases, case_count,
                    delivered_cases, note, created_at
                ) VALUES (?, ?, 'composed', ?, ?, 0, ?, ?)
                """,
                (insert_no, output_variety, total, total, note, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(409, f"新批次号已存在：{provided_no or insert_no}") from exc
        new_id = int(cur.lastrowid)
        if not provided_no:
            final_no = f"M-{new_id:06d}"
            conn.execute("UPDATE inv_batches SET batch_no=? WHERE id=?", (final_no, new_id))
            insert_no = final_no

        comp_cur = conn.execute(
            """
            INSERT INTO inv_compositions(new_batch_id, total_cases, note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (new_id, total, note, now),
        )
        composition_id = int(comp_cur.lastrowid)

        for spec in specs:
            conn.execute(
                """
                INSERT INTO inv_composition_items(
                    composition_id, source_batch_id, cases
                ) VALUES (?, ?, ?)
                """,
                (composition_id, spec["batch_id"], spec["cases"]),
            )
            # 余量留在原批次；只扣减本次拆出的箱数，总在库箱数不增加。
            conn.execute(
                "UPDATE inv_batches SET case_count=case_count-? WHERE id=?",
                (spec["cases"], spec["batch_id"]),
            )
            # 每个新批次箱都接触该来源的全部祖先来源。
            conn.execute(
                """
                INSERT OR IGNORE INTO inv_batch_sources(batch_id, source_batch_id)
                SELECT ?, source_batch_id
                FROM inv_batch_sources
                WHERE batch_id=?
                """,
                (new_id, spec["batch_id"]),
            )

        conn.execute(
            "INSERT OR IGNORE INTO inv_batch_sources(batch_id, source_batch_id) VALUES (?, ?)",
            (new_id, new_id),
        )
        row = conn.execute("SELECT * FROM inv_batches WHERE id=?", (new_id,)).fetchone()
        return _batch_out(conn, row)


def compose_batches_from_payload(db_path: str, body: dict[str, Any],
                                 path_batch_ref: Any = None) -> dict[str, Any]:
    explicit_sources = _first(body, *SOURCE_LIST_KEYS)
    if path_batch_ref is not None and explicit_sources is None:
        # 路径已经指明唯一来源；避免 body 中冗余 batch_id 与路径被重复计算。
        cases = _first(body, *CASE_KEYS)
        if cases is None:
            raise ServiceError(422, "必须提供从该批次拆出的箱数 cases")
        specs = [{"batch_ref": path_batch_ref, "cases": cases}]
    else:
        specs = specs_from_body(body, source=True)
        if path_batch_ref is not None and explicit_sources is not None:
            path_cases = _first(body, "path_cases", "this_batch_cases",
                                "path_batch_cases")
            if path_cases is not None:
                specs.append({"batch_ref": path_batch_ref, "cases": path_cases})
    if not specs:
        raise ServiceError(422, "必须提供至少一个来源批次及拆出箱数 sources")

    new_no = _first(body, "new_batch_no", "new_batch_number", "new_lot_no",
                    "target_batch_no", "output_batch_no", "new_code", "new_number")
    variety = _first(body, "variety", "product_variety", "cultivar", "crop")
    return compose_batches(
        db_path,
        specs,
        new_batch_no=new_no,
        variety=variety,
        note=note_from_body(body),
    )


# 常见命名别名。
split_batches = compose_batches
merge_batches = compose_batches
split_and_merge = compose_batches
create_composed_batch = compose_batches


# ---------------------------------------------------------------------------
# 出库单与分配
# ---------------------------------------------------------------------------
def create_outbound_order(
    db_path: str,
    buyer_name: str,
    variety: str,
    total_cases: Any,
    *,
    buyer_code: str = "",
    order_no: str | None = None,
    note: str = "",
    allocations: list[dict[str, Any]] | None = None,
    prepare: bool = False,
) -> dict[str, Any]:
    buyer_name = clean_text(buyer_name, "买家", required=True)
    variety = clean_text(variety, "品种", required=True)
    total = positive_int(total_cases, "总箱数")
    buyer_code = clean_text(buyer_code, "买家编号", max_length=64)
    provided_no = clean_text(order_no, "出库单号", max_length=64) if order_no else ""
    now = _now()

    with tx(db_path) as conn:
        if provided_no:
            existing = conn.execute(
                "SELECT id FROM inv_outbound_orders WHERE order_no=?", (provided_no,)
            ).fetchone()
            if existing is not None:
                raise ServiceError(409, f"出库单号已存在：{provided_no}")
            insert_no = provided_no
        else:
            insert_no = f"TMP-{uuid.uuid4().hex}"
        try:
            cur = conn.execute(
                """
                INSERT INTO inv_outbound_orders(
                    order_no, buyer_name, buyer_code, variety, total_cases,
                    status, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (insert_no, buyer_name, buyer_code, variety, total, note, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(409, f"出库单号已存在：{provided_no or insert_no}") from exc
        oid = int(cur.lastrowid)
        if not provided_no:
            final_no = f"OUT-{oid:06d}"
            conn.execute("UPDATE inv_outbound_orders SET order_no=? WHERE id=?", (final_no, oid))

        if allocations:
            raw_specs = normalize_specs(allocations)
            _replace_allocations(conn, oid, raw_specs)

        row = conn.execute("SELECT * FROM inv_outbound_orders WHERE id=?", (oid,)).fetchone()
        result = _order_out(conn, row)

    if prepare:
        result = prepare_order(db_path, oid)
    return result


def create_order_from_payload(db_path: str, body: dict[str, Any]) -> dict[str, Any]:
    buyer_name, buyer_code = buyer_from_body(body)
    buyer_name, buyer_code = _resolve_buyer_reference(
        db_path, body, buyer_name, buyer_code
    )
    variety = variety_from_body(body)
    total = _first(
        body, "total_cases", "total_boxes", "total_quantity", "total_qty",
        *CASE_KEYS,
    )
    if total is None:
        raise ServiceError(422, "必须提供总箱数 total_cases")
    order_no = _first(body, "order_no", "outbound_no", "shipment_no", "delivery_no",
                      "order_number", "code", "number")
    allocations = specs_from_body(body)
    prepare = bool(_first(body, "prepare", "auto_prepare", "reserve", "ready") or False)
    # status=prepared/allocated/ready 也表示创建并直接备货。
    requested_status = clean_text(_first(body, "status", "state") or "", "状态").lower()
    if requested_status in ("prepared", "allocated", "ready", "reserved"):
        prepare = True
    return create_outbound_order(
        db_path,
        buyer_name,
        variety,
        total,
        buyer_code=buyer_code,
        order_no=order_no,
        note=note_from_body(body),
        allocations=allocations,
        prepare=prepare,
    )


def list_orders(db_path: str, *, status: str | None = None,
                buyer: str | None = None, variety: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM inv_outbound_orders WHERE 1=1"
    params: list[Any] = []
    if status:
        normalized = _normalize_status_filter(status)
        if normalized == "canceled":
            normalized = "cancelled"
        sql += " AND status=?"
        params.append(normalized)
    if buyer:
        sql += " AND buyer_name LIKE ?"
        params.append(f"%{str(buyer).strip()}%")
    if variety:
        sql += " AND variety=?"
        params.append(str(variety).strip())
    sql += " ORDER BY id DESC"
    with read(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_order_out(conn, r) for r in rows]


def _normalize_status_filter(status: str) -> str:
    text = str(status).strip().lower().replace("-", "_")
    mapping = {
        "draft": "pending",
        "new": "pending",
        "waiting": "pending",
        "to_prepare": "pending",
        "pending_preparation": "pending",
        "allocated": "prepared",
        "reserve": "prepared",
        "reserved": "prepared",
        "ready": "prepared",
        "stocked": "prepared",
        "shipped": "delivered",
        "delivery": "delivered",
        "done": "delivered",
        "complete": "delivered",
        "completed": "delivered",
        "canceled": "cancelled",
    }
    return mapping.get(text, text)


def get_order(db_path: str, order_ref: Any) -> dict[str, Any]:
    with read(db_path) as conn:
        return _order_out(conn, _get_order_row(conn, order_ref))


def _replace_allocations(conn: sqlite3.Connection, order_id: int,
                         raw_specs: list[dict[str, Any]]) -> list[sqlite3.Row]:
    order = conn.execute(
        "SELECT * FROM inv_outbound_orders WHERE id=?", (order_id,)
    ).fetchone()
    if order is None:
        raise ServiceError(404, "出库单不存在")
    if order["status"] != "pending":
        label = ORDER_STATUS_LABELS.get(order["status"], order["status"])
        raise ServiceError(409, f"出库单当前为{label}，不能修改批次分配")

    specs = _resolve_source_specs(conn, raw_specs, expected_variety=order["variety"])
    total_alloc = sum(s["cases"] for s in specs)
    if total_alloc > int(order["total_cases"]):
        raise ServiceError(
            422,
            f"分配箱数 {total_alloc} 超过出库单总箱数 {order['total_cases']}",
        )

    now = _now()
    conn.execute("DELETE FROM inv_outbound_allocations WHERE order_id=?", (order_id,))
    for spec in specs:
        conn.execute(
            """
            INSERT INTO inv_outbound_allocations(
                order_id, batch_id, cases, batch_no_snapshot,
                variety_snapshot, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (order_id, spec["batch_id"], spec["cases"], spec["batch_no"],
             spec["variety"], now, now),
        )
    conn.execute("UPDATE inv_outbound_orders SET updated_at=? WHERE id=?", (now, order_id))
    return conn.execute(
        "SELECT * FROM inv_outbound_allocations WHERE order_id=? ORDER BY id",
        (order_id,),
    ).fetchall()


def save_allocations(db_path: str, order_ref: Any,
                     allocations: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    raw_specs = normalize_specs(allocations if allocations is not None else [])
    with tx(db_path) as conn:
        order = _get_order_row(conn, order_ref)
        _replace_allocations(conn, int(order["id"]), raw_specs)
        row = conn.execute(
            "SELECT * FROM inv_outbound_orders WHERE id=?", (order["id"],)
        ).fetchone()
        return _order_out(conn, row)


def add_allocation(db_path: str, order_ref: Any, body_or_spec: dict[str, Any]) -> dict[str, Any]:
    """追加或更新其中一个批次分配，不删除其它批次。"""
    with tx(db_path) as conn:
        order = _get_order_row(conn, order_ref)
        oid = int(order["id"])
        if order["status"] != "pending":
            raise ServiceError(409, "只有待备货订单可以修改批次分配")
        existing = conn.execute(
            "SELECT * FROM inv_outbound_allocations WHERE order_id=? ORDER BY id",
            (oid,),
        ).fetchall()
        current = [
            {"batch_id": r["batch_id"], "cases": r["cases"]} for r in existing
        ]
        new_specs = _resolve_source_specs(conn, specs_from_body(body_or_spec))
        by_id = {s["batch_id"]: s["cases"] for s in new_specs}
        merged: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in current:
            bid = int(item["batch_id"])
            merged.append({"batch_id": bid, "cases": by_id.get(bid, item["cases"])})
            seen.add(bid)
        for bid, qty in by_id.items():
            if bid not in seen:
                merged.append({"batch_id": bid, "cases": qty})
        _replace_allocations(conn, oid, merged)
        row = conn.execute("SELECT * FROM inv_outbound_orders WHERE id=?", (oid,)).fetchone()
        return _order_out(conn, row)


def _prepare_in_tx(conn: sqlite3.Connection, order: sqlite3.Row,
                   raw_specs: list[dict[str, Any]] | None) -> sqlite3.Row:
    oid = int(order["id"])
    now = _now()

    if order["status"] == "delivered":
        raise ServiceError(409, "出库单已交货，不能重复备货")
    if order["status"] == "cancelled":
        raise ServiceError(409, "出库单已取消，不能备货")

    if order["status"] == "prepared":
        if raw_specs is not None:
            requested = _resolve_source_specs(
                conn, raw_specs, expected_variety=order["variety"]
            )
            existing_rows = conn.execute(
                "SELECT batch_id, cases FROM inv_outbound_allocations WHERE order_id=?",
                (oid,),
            ).fetchall()
            existing = {int(r["batch_id"]): int(r["cases"]) for r in existing_rows}
            wanted = {s["batch_id"]: s["cases"] for s in requested}
            if existing != wanted:
                raise ServiceError(409, "出库单已按其它批次分配备货；请先取消/退回后再改配")
        return conn.execute("SELECT * FROM inv_outbound_orders WHERE id=?", (oid,)).fetchone()

    if raw_specs is not None:
        _replace_allocations(conn, oid, raw_specs)

    allocations = conn.execute(
        "SELECT * FROM inv_outbound_allocations WHERE order_id=? ORDER BY id",
        (oid,),
    ).fetchall()
    if not allocations:
        raise ServiceError(409, "尚未指定出库批次，不能备货")

    allocated = sum(int(r["cases"]) for r in allocations)
    if allocated != int(order["total_cases"]):
        raise ServiceError(
            409,
            f"批次箱数尚未凑齐：已指定 {allocated} / {order['total_cases']} 箱",
        )

    # 整单校验：隔离、库存、其它已备货单占用。任一项失败则不写 prepared。
    for alloc in allocations:
        bid = int(alloc["batch_id"])
        batch = conn.execute("SELECT * FROM inv_batches WHERE id=?", (bid,)).fetchone()
        if batch is None:
            raise ServiceError(409, "出库分配引用的批次不存在")
        events = _active_event_rows(conn, bid)
        if events:
            raise ServiceError(
                409,
                f"批次 {alloc['batch_no_snapshot']} 正在隔离中，不能备货",
            )
        reserved_other = _reserved_cases(conn, bid, exclude_order_id=oid)
        available = int(batch["case_count"]) - reserved_other
        if int(alloc["cases"]) > available:
            raise ServiceError(
                409,
                f"批次 {alloc['batch_no_snapshot']} 可用库存不足："
                f"本单需 {alloc['cases']} 箱，未被其它备货占用 {available} 箱",
            )

    conn.execute(
        "UPDATE inv_outbound_orders SET status='prepared', prepared_at=?, updated_at=? WHERE id=?",
        (now, now, oid),
    )
    return conn.execute("SELECT * FROM inv_outbound_orders WHERE id=?", (oid,)).fetchone()


def prepare_order(db_path: str, order_ref: Any,
                  allocations: list[dict[str, Any]] | dict[str, Any] | None = None) -> dict[str, Any]:
    raw_specs = normalize_specs(allocations) if allocations is not None else None
    with tx(db_path) as conn:
        order = _get_order_row(conn, order_ref)
        row = _prepare_in_tx(conn, order, raw_specs)
        return _order_out(conn, row)


reserve_order = prepare_order
stock_order = prepare_order


def prepare_order_from_payload(db_path: str, order_ref: Any,
                               body: dict[str, Any] | None) -> dict[str, Any]:
    body = body or {}
    specs = specs_from_body(body)
    return prepare_order(db_path, order_ref, specs if specs else None)


def cancel_order(db_path: str, order_ref: Any) -> dict[str, Any]:
    now = _now()
    with tx(db_path) as conn:
        order = _get_order_row(conn, order_ref)
        oid = int(order["id"])
        # 取消幂等；已取消单上的 prepared 占用早已通过状态排除。
        if order["status"] == "cancelled":
            return _order_out(conn, order)
        if order["status"] == "delivered":
            raise ServiceError(409, "已交货订单不能取消")
        conn.execute(
            "UPDATE inv_outbound_orders SET status='cancelled', cancelled_at=?, updated_at=? WHERE id=?",
            (now, now, oid),
        )
        row = conn.execute("SELECT * FROM inv_outbound_orders WHERE id=?", (oid,)).fetchone()
        return _order_out(conn, row)


def deliver_order(db_path: str, order_ref: Any, *, note: str = "") -> dict[str, Any]:
    now = _now()
    with tx(db_path) as conn:
        order = _get_order_row(conn, order_ref)
        oid = int(order["id"])

        # 重试确认交货：返回首次固化的实际交付明细，绝不二次扣库存。
        if order["status"] == "delivered":
            return _order_out(conn, order)
        if order["status"] == "cancelled":
            raise ServiceError(409, "已取消订单不能交货")
        if order["status"] == "pending":
            raise ServiceError(409, "订单尚未备货，不能确认交货")
        if order["status"] != "prepared":
            raise ServiceError(409, f"订单状态 {order['status']} 不能交货")

        allocations = conn.execute(
            "SELECT * FROM inv_outbound_allocations WHERE order_id=? ORDER BY id",
            (oid,),
        ).fetchall()
        if not allocations:
            raise ServiceError(409, "已备货订单缺少批次明细，不能交货")

        # 交货前再次做整单隔离和数量校验；正常情况下质量事件会先把受影响
        # 的已备货单整单退回，这里是并发/数据一致性的最后防线。
        for alloc in allocations:
            bid = int(alloc["batch_id"])
            events = _active_event_rows(conn, bid)
            if events:
                raise ServiceError(
                    409,
                    f"批次 {alloc['batch_no_snapshot']} 正在隔离中，不能交货",
                )
            batch = conn.execute("SELECT case_count FROM inv_batches WHERE id=?", (bid,)).fetchone()
            if batch is None or int(batch["case_count"]) < int(alloc["cases"]):
                raise ServiceError(409, f"批次 {alloc['batch_no_snapshot']} 在库箱数不足")

        total = sum(int(r["cases"]) for r in allocations)
        if total != int(order["total_cases"]):
            raise ServiceError(409, "备货总箱数与出库单总箱数不一致，不能整单交货")

        delivery_cur = conn.execute(
            "INSERT INTO inv_deliveries(order_id, delivered_at, note) VALUES (?, ?, ?)",
            (oid, now, note),
        )
        delivery_id = int(delivery_cur.lastrowid)

        for alloc in allocations:
            bid = int(alloc["batch_id"])
            qty = int(alloc["cases"])
            update_cur = conn.execute(
                """
                UPDATE inv_batches
                SET case_count=case_count-?, delivered_cases=delivered_cases+?
                WHERE id=? AND case_count>=?
                """,
                (qty, qty, bid, qty),
            )
            if update_cur.rowcount != 1:
                raise ServiceError(409, "扣减批次库存失败")
            conn.execute(
                """
                INSERT INTO inv_delivery_items(
                    delivery_id, order_id, allocation_id, batch_id,
                    batch_no, variety, cases
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (delivery_id, oid, alloc["id"], bid,
                 alloc["batch_no_snapshot"], alloc["variety_snapshot"], qty),
            )

        conn.execute(
            "UPDATE inv_outbound_orders SET status='delivered', delivered_at=?, updated_at=? WHERE id=?",
            (now, now, oid),
        )
        row = conn.execute("SELECT * FROM inv_outbound_orders WHERE id=?", (oid,)).fetchone()
        return _order_out(conn, row)


confirm_delivery = deliver_order
ship_order = deliver_order


# ---------------------------------------------------------------------------
# 质量事件与召回
# ---------------------------------------------------------------------------
def create_quality_event(
    db_path: str,
    target_batch_ref: Any,
    reason: str = "",
    title: str = "",
    event_no: str | None = None,
    severity: str = "normal",
    *legacy_args: Any,
    **legacy_kwargs: Any,
) -> dict[str, Any]:
    title = legacy_kwargs.get("title", title) or ""
    reason = legacy_kwargs.get("reason", reason)
    if not reason:
        reason = legacy_kwargs.get("description", legacy_kwargs.get("note", ""))
    event_no = legacy_kwargs.get("event_no", event_no)
    severity = legacy_kwargs.get("severity", severity) or "normal"
    title = clean_text(title, "事件标题", max_length=200)
    reason = clean_text(reason, "质量问题说明", max_length=1000)
    severity = clean_text(severity or "normal", "严重级别", max_length=32) or "normal"
    provided_no = clean_text(event_no, "质量事件号", max_length=64) if event_no else ""
    now = _now()

    with tx(db_path) as conn:
        target = _get_batch_row(conn, target_batch_ref)
        target_id = int(target["id"])
        if provided_no:
            existing = conn.execute(
                "SELECT id FROM inv_quality_events WHERE event_no=?", (provided_no,)
            ).fetchone()
            if existing is not None:
                raise ServiceError(409, f"质量事件号已存在：{provided_no}")
            insert_no = provided_no
        else:
            insert_no = f"TMP-{uuid.uuid4().hex}"

        cur = conn.execute(
            """
            INSERT INTO inv_quality_events(
                event_no, target_batch_id, title, reason, severity,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?)
            """,
            (insert_no, target_id, title, reason, severity, now),
        )
        event_id = int(cur.lastrowid)
        if not provided_no:
            final_no = f"QE-{event_id:06d}"
            conn.execute("UPDATE inv_quality_events SET event_no=? WHERE id=?", (final_no, event_id))

        affected_ids = [
            int(r["batch_id"])
            for r in conn.execute(
                "SELECT batch_id FROM inv_batch_sources WHERE source_batch_id=?",
                (target_id,),
            ).fetchall()
        ]
        id_marks = ",".join("?" for _ in affected_ids)

        # 已备货未交货：任何一个受影响批次都使整单退回 pending，并通过状态
        # 改变释放该单全部批次（包括未受影响批次）的占用。
        prepared_orders = conn.execute(
            f"""
            SELECT DISTINCT o.*
            FROM inv_outbound_orders o
            JOIN inv_outbound_allocations a ON a.order_id=o.id
            WHERE o.status='prepared' AND a.batch_id IN ({id_marks})
            ORDER BY o.id
            """,
            affected_ids,
        ).fetchall()
        for order in prepared_orders:
            oid = int(order["id"])
            conn.execute(
                "UPDATE inv_outbound_orders SET status='pending', prepared_at=NULL, updated_at=? WHERE id=?",
                (now, oid),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO inv_event_order_actions(
                    event_id, order_id, action, created_at
                ) VALUES (?, ?, 'returned_to_pending', ?)
                """,
                (event_id, oid, now),
            )

        # 已交货：按交付明细生成不可被解除事件抹去的召回快照。
        delivered_items = conn.execute(
            f"""
            SELECT di.*, o.buyer_name, o.order_no
            FROM inv_delivery_items di
            JOIN inv_outbound_orders o ON o.id = di.order_id
            WHERE di.batch_id IN ({id_marks}) AND o.status='delivered'
            ORDER BY o.id, di.id
            """,
            affected_ids,
        ).fetchall()

        orders_by_id: dict[int, sqlite3.Row] = {}
        for item in delivered_items:
            orders_by_id.setdefault(int(item["order_id"]), item)

        for oid, item_row in orders_by_id.items():
            case_cur = conn.execute(
                """
                INSERT INTO inv_recall_cases(
                    event_id, order_id, order_no, buyer_name, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id, order_id) DO NOTHING
                """,
                (event_id, oid, item_row["order_no"], item_row["buyer_name"], now),
            )
            case_row = conn.execute(
                "SELECT id FROM inv_recall_cases WHERE event_id=? AND order_id=?",
                (event_id, oid),
            ).fetchone()
            case_id = int(case_row["id"])
            order_items = [r for r in delivered_items if int(r["order_id"]) == oid]
            for di in order_items:
                # UNIQUE(event_id, delivery_item_id) 保证同一交付明细即使
                # 经多条来源路径受影响，也只形成一条召回明细。
                conn.execute(
                    """
                    INSERT OR IGNORE INTO inv_recall_items(
                        case_id, event_id, order_id, delivery_item_id,
                        batch_id, batch_no, variety, cases
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (case_id, event_id, oid, int(di["id"]), int(di["batch_id"]),
                     di["batch_no"], di["variety"], int(di["cases"])),
                )

        event_row = conn.execute(
            "SELECT * FROM inv_quality_events WHERE id=?", (event_id,)
        ).fetchone()
        return _event_out(conn, event_row)


def create_quality_event_from_payload(
    db_path: str,
    body: dict[str, Any],
    path_batch_ref: Any = None,
) -> dict[str, Any]:
    target = path_batch_ref
    if target is None:
        target = _first(body, "batch_ref", "batch_id", "target_batch_id", *BATCH_NO_KEYS)
    if target is None or isinstance(target, (dict, list)):
        spec = _batch_ref_from_dict(body)
        target = spec.get("batch_id") or spec.get("batch_no") or spec.get("batch_ref")
    if isinstance(target, dict):
        spec = _batch_ref_from_dict({"batch": target})
        target = spec.get("batch_id") or spec.get("batch_no") or spec.get("batch_ref")
    if target is None:
        raise ServiceError(422, "必须提供被登记质量事件的目标批次")
    title = _first(body, "title", "event_title", "name") or ""
    reason = _first(body, "reason", "description", "detail", "details",
                    "issue", "problem", "note", "notes") or ""
    event_no = _first(body, "event_no", "quality_event_no", "incident_no",
                      "case_no", "code", "number")
    severity = _first(body, "severity", "level", "risk_level") or "normal"
    return create_quality_event(
        db_path,
        target,
        title=title,
        reason=reason,
        event_no=event_no,
        severity=severity,
    )


register_quality_event = create_quality_event
raise_quality_event = create_quality_event
quarantine_batch = create_quality_event


def list_quality_events(db_path: str, *, active_only: bool = False,
                        status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM inv_quality_events WHERE 1=1"
    params: list[Any] = []
    if active_only:
        sql += " AND status='active'"
    elif status:
        text = str(status).strip().lower()
        if text in ("open", "quarantined", "ongoing"):
            text = "active"
        elif text in ("released", "closed", "lifted", "done"):
            text = "resolved"
        sql += " AND status=?"
        params.append(text)
    sql += " ORDER BY id DESC"
    with read(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_event_out(conn, r) for r in rows]


def get_quality_event(db_path: str, event_ref: Any) -> dict[str, Any]:
    with read(db_path) as conn:
        return _event_out(conn, _get_event_row(conn, event_ref))


def resolve_quality_event(db_path: str, event_ref: Any,
                          resolution_note: str = "") -> dict[str, Any]:
    now = _now()
    note = clean_text(resolution_note or "", "解除说明", max_length=1000)
    with tx(db_path) as conn:
        row = _get_event_row(conn, event_ref)
        if row["status"] == "resolved":
            return _event_out(conn, row)
        conn.execute(
            """
            UPDATE inv_quality_events
            SET status='resolved', resolved_at=?, resolution_note=?
            WHERE id=?
            """,
            (now, note, row["id"]),
        )
        row = conn.execute(
            "SELECT * FROM inv_quality_events WHERE id=?", (row["id"],)
        ).fetchone()
        # 不修改 inv_recall_cases/items：召回清单与联系状态继续保留。
        return _event_out(conn, row)


release_quality_event = resolve_quality_event
close_quality_event = resolve_quality_event
lift_quality_event = resolve_quality_event


# ---------------------------------------------------------------------------
# 召回查询与联系状态
# ---------------------------------------------------------------------------
def list_recalls(
    db_path: str,
    *,
    event_ref: Any = None,
    order_ref: Any = None,
    contacted: bool | None = None,
    buyer: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT rc.* FROM inv_recall_cases rc WHERE 1=1"
    params: list[Any] = []
    with read(db_path) as conn:
        if event_ref is not None:
            event = _get_event_row(conn, event_ref)
            sql += " AND rc.event_id=?"
            params.append(event["id"])
        if order_ref is not None:
            order = _get_order_row(conn, order_ref)
            sql += " AND rc.order_id=?"
            params.append(order["id"])
        if contacted is not None:
            sql += " AND rc.contacted=?"
            params.append(1 if contacted else 0)
        if buyer:
            sql += " AND rc.buyer_name LIKE ?"
            params.append(f"%{str(buyer).strip()}%")
        sql += " ORDER BY rc.id DESC"
        rows = conn.execute(sql, params).fetchall()
        return [_recall_out(conn, r) for r in rows]


def get_recall(db_path: str, recall_ref: Any) -> dict[str, Any]:
    with read(db_path) as conn:
        if isinstance(recall_ref, int) or (isinstance(recall_ref, str) and recall_ref.isdigit()):
            row = conn.execute(
                "SELECT * FROM inv_recall_cases WHERE id=?", (int(recall_ref),)
            ).fetchone()
            if row is not None:
                return _recall_out(conn, row)
        text = str(recall_ref)
        row = conn.execute(
            "SELECT * FROM inv_recall_cases WHERE id=?", (text,)
        ).fetchone() if text.isdigit() else None
        if row is None:
            raise ServiceError(404, f"召回记录不存在：{recall_ref}")
        return _recall_out(conn, row)


def _find_recall_case(
    conn: sqlite3.Connection,
    recall_ref: Any = None,
    event_ref: Any = None,
    order_ref: Any = None,
) -> sqlite3.Row:
    if event_ref is not None and order_ref is not None:
        event = _get_event_row(conn, event_ref)
        order = _get_order_row(conn, order_ref)
        row = conn.execute(
            "SELECT * FROM inv_recall_cases WHERE event_id=? AND order_id=?",
            (event["id"], order["id"]),
        ).fetchone()
        if row is None:
            raise ServiceError(404, "该质量事件没有此订单的召回记录")
        return row

    if recall_ref is not None:
        text = str(recall_ref).strip()
        row = conn.execute(
            "SELECT * FROM inv_recall_cases WHERE id=?",
            (int(recall_ref) if isinstance(recall_ref, int) or text.isdigit() else -1,),
        ).fetchone() if isinstance(recall_ref, int) or text.isdigit() else None
        if row is not None:
            return row

        # 兼容把 order_id / order_no 直接放到 /admin/recalls/{id}/contact。
        order = None
        try:
            order = _get_order_row(conn, recall_ref)
        except ServiceError:
            order = None
        if order is not None:
            if event_ref is not None:
                event = _get_event_row(conn, event_ref)
                row = conn.execute(
                    "SELECT * FROM inv_recall_cases WHERE event_id=? AND order_id=?",
                    (event["id"], order["id"]),
                ).fetchone()
                if row is not None:
                    return row
            rows = conn.execute(
                "SELECT * FROM inv_recall_cases WHERE order_id=? ORDER BY id",
                (order["id"],),
            ).fetchall()
            if len(rows) == 1:
                return rows[0]
            if len(rows) > 1:
                ids = [r["id"] for r in rows]
                raise ServiceError(
                    409,
                    f"该订单涉及多个质量事件，请用 event_id 指定；召回记录：{ids}",
                )

    raise ServiceError(404, f"召回记录不存在：{recall_ref}")


def contact_recall(
    db_path: str,
    recall_ref: Any = None,
    contacted: bool = True,
    note: str | None = None,
    contacted_by: str | None = None,
    event_ref: Any = None,
    order_ref: Any = None,
) -> dict[str, Any]:
    now = _now()
    with tx(db_path) as conn:
        row = _find_recall_case(
            conn, recall_ref=recall_ref, event_ref=event_ref, order_ref=order_ref
        )
        if contacted:
            previous_count = int(row["contact_count"])
            new_note = row["contact_note"] if note is None else clean_text(note, "联系备注", max_length=500)
            new_by = row["contacted_by"] if contacted_by is None else clean_text(
                contacted_by, "联系人", max_length=128
            )
            conn.execute(
                """
                UPDATE inv_recall_cases
                SET contacted=1,
                    contact_count=?,
                    contacted_at=COALESCE(contacted_at, ?),
                    last_contacted_at=?,
                    contact_note=?,
                    contacted_by=?
                WHERE id=?
                """,
                (previous_count + 1, now, now, new_note, new_by, row["id"]),
            )
        else:
            new_note = row["contact_note"] if note is None else clean_text(note, "联系备注", max_length=500)
            conn.execute(
                """
                UPDATE inv_recall_cases
                SET contacted=0, contacted_at=NULL, contact_note=?
                WHERE id=?
                """,
                (new_note, row["id"]),
            )
        row = conn.execute(
            "SELECT * FROM inv_recall_cases WHERE id=?", (row["id"],)
        ).fetchone()
        return _recall_out(conn, row)


def contact_recall_from_payload(
    db_path: str,
    body: dict[str, Any] | None,
    *,
    recall_ref: Any = None,
    event_ref: Any = None,
    order_ref: Any = None,
) -> dict[str, Any]:
    body = body or {}
    contacted_value = _first(body, "contacted", "is_contacted", "done", "reached")
    contacted = True if contacted_value is None else bool(contacted_value)
    note = _first(body, "note", "contact_note", "remark", "result")
    by = _first(body, "contacted_by", "contact_by", "operator", "admin", "name")
    body_event = _first(body, "event_id", "quality_event_id", "event_no")
    body_order = _first(body, "order_id", "outbound_order_id", "order_no")
    return contact_recall(
        db_path,
        recall_ref,
        event_ref=event_ref if event_ref is not None else body_event,
        order_ref=order_ref if order_ref is not None else body_order,
        contacted=contacted,
        note=note,
        contacted_by=by,
    )


def inventory_summary(db_path: str) -> dict[str, Any]:
    with read(db_path) as conn:
        batches = conn.execute("SELECT * FROM inv_batches ORDER BY id").fetchall()
        orders = conn.execute("SELECT * FROM inv_outbound_orders ORDER BY id").fetchall()
        events = conn.execute("SELECT * FROM inv_quality_events ORDER BY id").fetchall()
        batch_outs = [_batch_out(conn, b) for b in batches]
        order_outs = [_order_out(conn, o) for o in orders]
        event_outs = [_event_out(conn, e) for e in events]
        total_on_hand = sum(int(b["case_count"]) for b in batches)
        total_delivered = sum(int(b["delivered_cases"]) for b in batches)
        total_inbound = sum(
            int(b["initial_cases"]) for b in batches if b["origin"] == "inbound"
        )
        return {
            "batch_count": len(batches),
            "order_count": len(orders),
            "active_event_count": sum(1 for e in events if e["status"] == "active"),
            "total_inbound_cases": total_inbound,
            "total_on_hand_cases": total_on_hand,
            "total_delivered_cases": total_delivered,
            "total_accounted_cases": total_on_hand + total_delivered,
            "inventory_invariant_ok": total_inbound == total_on_hand + total_delivered,
            "batches": batch_outs,
            "orders": order_outs,
            "quality_events": event_outs,
        }


# ---------------------------------------------------------------------------
# 额外服务函数命名别名，方便不同调用习惯与直接服务层测试。
# ---------------------------------------------------------------------------
receive_batch = create_batch
register_batch = create_batch
inbound_batch = create_batch
add_batch = create_batch
split_batch = compose_batches
merge_batch = compose_batches

create_shipment = create_outbound_order
create_delivery_order = create_outbound_order
prepare_shipment = prepare_order
reserve_shipment = prepare_order
deliver_shipment = deliver_order
ship_order = deliver_order
confirm_shipment = deliver_order
cancel_shipment = cancel_order

register_incident = create_quality_event
create_quality_incident = create_quality_event
resolve_incident = resolve_quality_event
release_quarantine = resolve_quality_event
mark_recall_contacted = contact_recall


def batch_active_events(db_path: str, batch_ref: Any) -> list[dict[str, Any]]:
    """返回当前仍生效、会隔离指定批次的质量事件。"""
    with read(db_path) as conn:
        batch = _get_batch_row(conn, batch_ref)
        return [_event_brief(e) for e in _active_event_rows(conn, int(batch["id"]))]


def is_batch_quarantined(db_path: str, batch_ref: Any) -> bool:
    return bool(batch_active_events(db_path, batch_ref))


def available_cases(db_path: str, batch_ref: Any) -> int:
    with read(db_path) as conn:
        batch = _get_batch_row(conn, batch_ref)
        return int(batch["case_count"]) - _reserved_cases(conn, int(batch["id"]))


def batch_lineage(db_path: str, batch_ref: Any) -> dict[str, Any]:
    """返回批次的全部祖先来源与直接/间接后代。"""
    with read(db_path) as conn:
        batch = _get_batch_row(conn, batch_ref)
        bid = int(batch["id"])
        ancestors = [
            _batch_out(conn, r)
            for r in conn.execute(
                """
                SELECT b.*
                FROM inv_batch_sources s
                JOIN inv_batches b ON b.id = s.source_batch_id
                WHERE s.batch_id=? AND s.source_batch_id <> ?
                ORDER BY b.id
                """,
                (bid, bid),
            ).fetchall()
        ]
        descendants = [
            _batch_out(conn, r)
            for r in conn.execute(
                """
                SELECT b.*
                FROM inv_batch_sources s
                JOIN inv_batches b ON b.id = s.batch_id
                WHERE s.source_batch_id=? AND s.batch_id <> ?
                ORDER BY b.id
                """,
                (bid, bid),
            ).fetchall()
        ]
        return {
            "batch": _batch_out(conn, batch),
            "ancestors": ancestors,
            "sources": ancestors,
            "descendants": descendants,
            "children": descendants,
            "ancestor_batch_ids": [b["id"] for b in ancestors],
            "descendant_batch_ids": [b["id"] for b in descendants],
        }


def contact_order_for_event(
    db_path: str,
    event_ref: Any,
    order_ref: Any,
    note: str | None = None,
    contacted_by: str | None = None,
    contacted: bool = True,
) -> dict[str, Any]:
    """按质量事件和出库单记录独立联系状态。"""
    return contact_recall(
        db_path,
        None,
        contacted=contacted,
        note=note,
        contacted_by=contacted_by,
        event_ref=event_ref,
        order_ref=order_ref,
    )
