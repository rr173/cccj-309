"""批次库存、出库与召回的管理员 HTTP 接口。

为兼容常见本地调用习惯，同一组服务操作注册了 batches/lots/orders/
shipments/quality-events/incidents 等路径别名；规范字段之外也接受
batch_number、quantity、qty、customer 等别名。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from . import inventory
from .security import authenticate_member
from .service import ServiceError


def require_admin(member=Depends(authenticate_member)):
    if member is None:
        raise ServiceError(401, "缺少或无效的会员令牌")
    if not member["is_admin"]:
        raise ServiceError(403, "需要名册管理员权限")
    return member


router = APIRouter(tags=["inventory"], dependencies=[Depends(require_admin)])


def _db(request: Request) -> str:
    return request.app.state.settings.db_path


def _body(body: dict[str, Any] | None) -> dict[str, Any]:
    return body or {}


# ---------------------------------------------------------------------------
# 入库 / 批次查询
# ---------------------------------------------------------------------------
@router.post("/admin/batches", status_code=201)
@router.post("/admin/batches/receive", status_code=201, include_in_schema=False)
@router.post("/admin/batches/intake", status_code=201, include_in_schema=False)
@router.post("/admin/batches/inbound", status_code=201, include_in_schema=False)
@router.post("/admin/batches/register", status_code=201, include_in_schema=False)
@router.post("/admin/inbound", status_code=201, include_in_schema=False)
@router.post("/admin/inventory/inbound", status_code=201, include_in_schema=False)
@router.post("/admin/inventory/stock-in", status_code=201, include_in_schema=False)
@router.post("/admin/lots", status_code=201, include_in_schema=False)
@router.post("/admin/lots/receive", status_code=201, include_in_schema=False)
@router.post("/admin/inventory/batches", status_code=201, include_in_schema=False)
@router.post("/admin/inventory/receive", status_code=201, include_in_schema=False)
@router.post("/admin/inventory/lots", status_code=201, include_in_schema=False)
def create_batch(request: Request, body: dict[str, Any] | None = None):
    return inventory.create_batch_from_payload(_db(request), _body(body))


@router.get("/admin/batches")
@router.get("/admin/lots", include_in_schema=False)
@router.get("/admin/inventory/batches", include_in_schema=False)
def list_batches(
    request: Request,
    variety: str | None = None,
    quarantined: bool | None = None,
    available_only: bool = False,
):
    return inventory.list_batches(
        _db(request),
        variety=variety,
        quarantined=quarantined,
        available_only=available_only,
    )


@router.get("/admin/inventory/summary")
@router.get("/admin/inventory", include_in_schema=False)
def inventory_summary(request: Request):
    return inventory.inventory_summary(_db(request))


@router.post("/admin/batches/split", status_code=201)
@router.post("/admin/batches/merge", status_code=201)
@router.post("/admin/split", status_code=201, include_in_schema=False)
@router.post("/admin/merge", status_code=201, include_in_schema=False)
@router.post("/admin/inventory/split", status_code=201, include_in_schema=False)
@router.post("/admin/inventory/merge", status_code=201, include_in_schema=False)
@router.post("/admin/batches/compose", status_code=201, include_in_schema=False)
@router.post("/admin/batches/split-merge", status_code=201, include_in_schema=False)
@router.post("/admin/lots/split", status_code=201, include_in_schema=False)
@router.post("/admin/lots/merge", status_code=201, include_in_schema=False)
def compose_batches(request: Request, body: dict[str, Any] | None = None):
    return inventory.compose_batches_from_payload(_db(request), _body(body))


@router.get("/admin/batches/{batch_ref}")
@router.get("/admin/lots/{batch_ref}", include_in_schema=False)
@router.get("/admin/inventory/batches/{batch_ref}", include_in_schema=False)
def get_batch(batch_ref: str, request: Request):
    return inventory.get_batch(_db(request), batch_ref)


@router.get("/admin/batches/{batch_ref}/lineage")
@router.get("/admin/lots/{batch_ref}/lineage", include_in_schema=False)
@router.get("/admin/inventory/batches/{batch_ref}/lineage", include_in_schema=False)
def get_batch_lineage(batch_ref: str, request: Request):
    return inventory.batch_lineage(_db(request), batch_ref)


@router.post("/admin/batches/{batch_ref}/split", status_code=201)
@router.post("/admin/batches/{batch_ref}/merge", status_code=201)
@router.post("/admin/batches/{batch_ref}/compose", status_code=201, include_in_schema=False)
@router.post("/admin/lots/{batch_ref}/split", status_code=201, include_in_schema=False)
def split_named_batch(batch_ref: str, request: Request, body: dict[str, Any] | None = None):
    return inventory.compose_batches_from_payload(
        _db(request), _body(body), path_batch_ref=batch_ref
    )


# ---------------------------------------------------------------------------
# 出库单
# ---------------------------------------------------------------------------
@router.post("/admin/outbound-orders", status_code=201)
@router.post("/admin/orders", status_code=201, include_in_schema=False)
@router.post("/admin/shipments", status_code=201, include_in_schema=False)
@router.post("/admin/delivery-orders", status_code=201, include_in_schema=False)
@router.post("/admin/outbounds", status_code=201, include_in_schema=False)
@router.post("/admin/inventory/orders", status_code=201, include_in_schema=False)
@router.post("/admin/inventory/outbound-orders", status_code=201, include_in_schema=False)
def create_order(request: Request, body: dict[str, Any] | None = None):
    return inventory.create_order_from_payload(_db(request), _body(body))


@router.get("/admin/outbound-orders")
@router.get("/admin/orders", include_in_schema=False)
@router.get("/admin/shipments", include_in_schema=False)
@router.get("/admin/delivery-orders", include_in_schema=False)
@router.get("/admin/outbounds", include_in_schema=False)
@router.get("/admin/inventory/orders", include_in_schema=False)
@router.get("/admin/inventory/outbound-orders", include_in_schema=False)
def list_orders(
    request: Request,
    status: str | None = None,
    buyer: str | None = None,
    buyer_name: str | None = None,
    customer: str | None = None,
    variety: str | None = None,
):
    return inventory.list_orders(
        _db(request),
        status=status,
        buyer=buyer or buyer_name or customer,
        variety=variety,
    )


@router.get("/admin/outbound-orders/{order_ref}")
@router.get("/admin/orders/{order_ref}", include_in_schema=False)
@router.get("/admin/shipments/{order_ref}", include_in_schema=False)
@router.get("/admin/delivery-orders/{order_ref}", include_in_schema=False)
@router.get("/admin/outbounds/{order_ref}", include_in_schema=False)
@router.get("/admin/inventory/orders/{order_ref}", include_in_schema=False)
@router.get("/admin/inventory/outbound-orders/{order_ref}", include_in_schema=False)
def get_order(order_ref: str, request: Request):
    return inventory.get_order(_db(request), order_ref)


async def _read_json(request: Request) -> dict[str, Any] | list[Any] | None:
    if not request.headers.get("content-length") and not request.headers.get("content-type"):
        return {}
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 - 空请求体按 {} 处理，格式错误交给参数校验
        return {}


async def _save_allocations_route(order_ref: str, request: Request):
    payload = await _read_json(request)
    if isinstance(payload, list):
        specs = inventory.normalize_specs(payload)
    elif isinstance(payload, dict):
        specs = inventory.specs_from_body(payload)
    else:
        specs = []
    if not specs:
        raise ServiceError(422, "必须提供 allocations 批次分配数组")
    return await run_in_threadpool(
        inventory.save_allocations, _db(request), order_ref, specs
    )


for _path in (
    "/admin/outbound-orders/{order_ref}/allocations",
    "/admin/orders/{order_ref}/allocations",
    "/admin/shipments/{order_ref}/allocations",
    "/admin/inventory/orders/{order_ref}/allocations",
):
    router.add_api_route(
        _path, _save_allocations_route, methods=["PUT", "PATCH"],
        include_in_schema=(_path.startswith("/admin/outbound-orders")),
    )


@router.post("/admin/outbound-orders/{order_ref}/allocations")
@router.post("/admin/orders/{order_ref}/allocations", include_in_schema=False)
@router.post("/admin/shipments/{order_ref}/allocations", include_in_schema=False)
async def add_allocation(order_ref: str, request: Request):
    payload = await _read_json(request)
    if isinstance(payload, list):
        specs = inventory.normalize_specs(payload)
        return await run_in_threadpool(
            inventory.save_allocations, _db(request), order_ref, specs
        )
    data = payload if isinstance(payload, dict) else {}
    # 若调用方一次提交完整数组，则按覆盖语义处理；单条对象按 upsert 处理。
    specs = inventory.specs_from_body(data)
    wrapper = inventory._first(data, "allocations", *inventory.ALLOCATION_LIST_KEYS)
    if len(specs) > 1 or wrapper is not None:
        return await run_in_threadpool(
            inventory.save_allocations, _db(request), order_ref, specs
        )
    return await run_in_threadpool(
        inventory.add_allocation, _db(request), order_ref, data
    )


@router.put("/admin/outbound-orders/{order_ref}")
@router.put("/admin/orders/{order_ref}", include_in_schema=False)
def replace_order_by_body(order_ref: str, request: Request, body: dict[str, Any] | None = None):
    data = _body(body)
    specs = inventory.specs_from_body(data)
    if specs:
        return inventory.save_allocations(_db(request), order_ref, specs)
    return inventory.get_order(_db(request), order_ref)


async def _prepare_route(order_ref: str, request: Request):
    payload = await _read_json(request)
    if isinstance(payload, list):
        specs = inventory.normalize_specs(payload)
        return await run_in_threadpool(
            inventory.prepare_order, _db(request), order_ref, specs
        )
    data = payload if isinstance(payload, dict) else {}
    return await run_in_threadpool(
        inventory.prepare_order_from_payload, _db(request), order_ref, data
    )


@router.post("/admin/outbound-orders/{order_ref}/prepare")
@router.post("/admin/outbound-orders/{order_ref}/reserve", include_in_schema=False)
@router.post("/admin/outbound-orders/{order_ref}/stock", include_in_schema=False)
@router.post("/admin/orders/{order_ref}/prepare")
@router.post("/admin/orders/{order_ref}/reserve", include_in_schema=False)
@router.post("/admin/orders/{order_ref}/allocate")
@router.post("/admin/shipments/{order_ref}/prepare", include_in_schema=False)
@router.post("/admin/shipments/{order_ref}/allocate", include_in_schema=False)
async def prepare_order(order_ref: str, request: Request):
    return await _prepare_route(order_ref, request)


@router.post("/admin/outbound-orders/{order_ref}/allocate")
async def allocate_order(order_ref: str, request: Request):
    """指定批次并凑齐后备货；请求中可带 allocations 或裸数组。"""
    return await _prepare_route(order_ref, request)


@router.post("/admin/outbound-orders/{order_ref}/deliver")
@router.post("/admin/outbound-orders/{order_ref}/confirm-delivery", include_in_schema=False)
@router.post("/admin/outbound-orders/{order_ref}/ship", include_in_schema=False)
@router.post("/admin/orders/{order_ref}/deliver")
@router.post("/admin/orders/{order_ref}/confirm", include_in_schema=False)
@router.post("/admin/orders/{order_ref}/ship", include_in_schema=False)
@router.post("/admin/shipments/{order_ref}/deliver", include_in_schema=False)
@router.post("/admin/shipments/{order_ref}/confirm", include_in_schema=False)
def deliver_order(order_ref: str, request: Request, body: dict[str, Any] | None = None):
    note = inventory.note_from_body(_body(body)) if body else ""
    return inventory.deliver_order(_db(request), order_ref, note=note)


@router.post("/admin/outbound-orders/{order_ref}/cancel")
@router.post("/admin/orders/{order_ref}/cancel")
@router.post("/admin/shipments/{order_ref}/cancel", include_in_schema=False)
@router.delete("/admin/outbound-orders/{order_ref}", include_in_schema=False)
@router.delete("/admin/orders/{order_ref}", include_in_schema=False)
def cancel_order(order_ref: str, request: Request):
    return inventory.cancel_order(_db(request), order_ref)


def _transition_order_by_body(order_ref: str, request: Request, data: dict[str, Any]):
    action = str(inventory._first(
        data, "action", "status", "state", "transition", "op"
    ) or "").strip().lower().replace("-", "_")
    if action in ("prepare", "prepared", "preparing", "reserve", "reserved",
                  "allocate", "allocated", "ready", "stock", "stocked"):
        return inventory.prepare_order_from_payload(_db(request), order_ref, data)
    if action in ("deliver", "delivered", "ship", "shipped", "confirm",
                  "confirmed", "complete", "completed", "done"):
        note = inventory.note_from_body(data)
        return inventory.deliver_order(_db(request), order_ref, note=note)
    if action in ("cancel", "cancelled", "canceled", "close", "closed"):
        return inventory.cancel_order(_db(request), order_ref)
    specs = inventory.specs_from_body(data)
    if specs:
        return inventory.save_allocations(_db(request), order_ref, specs)
    return inventory.get_order(_db(request), order_ref)


@router.patch("/admin/outbound-orders/{order_ref}")
@router.patch("/admin/orders/{order_ref}", include_in_schema=False)
@router.post("/admin/outbound-orders/{order_ref}/status", include_in_schema=False)
@router.post("/admin/orders/{order_ref}/status", include_in_schema=False)
def update_order(order_ref: str, request: Request, body: dict[str, Any] | None = None):
    return _transition_order_by_body(order_ref, request, _body(body))


# 直接接收 JSON 数组形式的 allocations（dict body 路由之外的补充）。
@router.put("/admin/outbound-orders/{order_ref}/allocation-list")
@router.post("/admin/outbound-orders/{order_ref}/allocation-list", include_in_schema=False)
async def save_allocation_list(order_ref: str, request: Request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = None
    specs = inventory.normalize_specs(payload)
    return await run_in_threadpool(
        inventory.save_allocations, _db(request), order_ref, specs
    )


# ---------------------------------------------------------------------------
# 质量事件
# ---------------------------------------------------------------------------
@router.post("/admin/quality-events", status_code=201)
@router.post("/admin/quality-incidents", status_code=201, include_in_schema=False)
@router.post("/admin/incidents", status_code=201, include_in_schema=False)
@router.post("/admin/quarantine-events", status_code=201, include_in_schema=False)
@router.post("/admin/inventory/quality-events", status_code=201, include_in_schema=False)
def create_quality_event(request: Request, body: dict[str, Any] | None = None):
    return inventory.create_quality_event_from_payload(_db(request), _body(body))


@router.post("/admin/batches/{batch_ref}/quality-events", status_code=201)
@router.post("/admin/lots/{batch_ref}/quality-events", status_code=201, include_in_schema=False)
@router.post("/admin/batches/{batch_ref}/incidents", status_code=201, include_in_schema=False)
@router.post("/admin/batches/{batch_ref}/quarantine", status_code=201, include_in_schema=False)
def create_batch_quality_event(
    batch_ref: str, request: Request, body: dict[str, Any] | None = None
):
    return inventory.create_quality_event_from_payload(
        _db(request), _body(body), path_batch_ref=batch_ref
    )


@router.get("/admin/batches/{batch_ref}/quarantine", include_in_schema=False)
def get_batch_quarantine(batch_ref: str, request: Request):
    events = inventory.batch_active_events(_db(request), batch_ref)
    return {"batch_ref": batch_ref, "quarantined": bool(events), "active_events": events}


@router.get("/admin/quality-events")
@router.get("/admin/quality-incidents", include_in_schema=False)
@router.get("/admin/incidents", include_in_schema=False)
def list_quality_events(request: Request, active_only: bool = False, status: str | None = None):
    return inventory.list_quality_events(
        _db(request), active_only=active_only, status=status
    )


@router.get("/admin/quality-events/{event_ref}")
@router.get("/admin/quality-incidents/{event_ref}", include_in_schema=False)
@router.get("/admin/incidents/{event_ref}", include_in_schema=False)
def get_quality_event(event_ref: str, request: Request):
    return inventory.get_quality_event(_db(request), event_ref)


@router.post("/admin/quality-events/{event_ref}/resolve")
@router.post("/admin/quality-events/{event_ref}/release", include_in_schema=False)
@router.post("/admin/quality-events/{event_ref}/close", include_in_schema=False)
@router.post("/admin/quality-events/{event_ref}/lift", include_in_schema=False)
@router.post("/admin/quality-incidents/{event_ref}/resolve", include_in_schema=False)
@router.post("/admin/quality-incidents/{event_ref}/release", include_in_schema=False)
def resolve_quality_event(event_ref: str, request: Request, body: dict[str, Any] | None = None):
    data = _body(body)
    note = inventory._first(data, "resolution_note", "note", "reason", "comment") or ""
    return inventory.resolve_quality_event(_db(request), event_ref, note)


@router.patch("/admin/quality-events/{event_ref}", include_in_schema=False)
@router.put("/admin/quality-events/{event_ref}", include_in_schema=False)
@router.post("/admin/quality-events/{event_ref}/status", include_in_schema=False)
def update_quality_event(event_ref: str, request: Request,
                         body: dict[str, Any] | None = None):
    data = _body(body)
    status = str(inventory._first(data, "status", "state", "action") or "").lower()
    if status in ("resolved", "released", "closed", "lifted", "resolve",
                  "release", "close", "lift"):
        note = inventory._first(data, "resolution_note", "note", "comment") or ""
        return inventory.resolve_quality_event(_db(request), event_ref, note)
    return inventory.get_quality_event(_db(request), event_ref)


# ---------------------------------------------------------------------------
# 召回与联系状态
# ---------------------------------------------------------------------------
@router.get("/admin/recalls")
@router.get("/admin/recall-cases", include_in_schema=False)
def list_recalls(
    request: Request,
    event_id: str | None = None,
    quality_event_id: str | None = None,
    order_id: str | None = None,
    contacted: bool | None = None,
    buyer: str | None = None,
    buyer_name: str | None = None,
):
    return inventory.list_recalls(
        _db(request),
        event_ref=event_id or quality_event_id,
        order_ref=order_id,
        contacted=contacted,
        buyer=buyer or buyer_name,
    )


@router.get("/admin/quality-events/{event_ref}/recalls")
@router.get("/admin/quality-events/{event_ref}/recall", include_in_schema=False)
def list_event_recalls(event_ref: str, request: Request, contacted: bool | None = None):
    return inventory.list_recalls(_db(request), event_ref=event_ref, contacted=contacted)


@router.post("/admin/recalls/contact")
def contact_recall_by_body(request: Request, body: dict[str, Any] | None = None):
    data = _body(body)
    return inventory.contact_recall_from_payload(
        _db(request),
        data,
        event_ref=inventory._first(data, "event_id", "quality_event_id", "event_no"),
        order_ref=inventory._first(data, "order_id", "outbound_order_id", "order_no"),
    )


@router.get("/admin/recalls/{recall_ref}")
@router.get("/admin/recall-cases/{recall_ref}", include_in_schema=False)
def get_recall(recall_ref: str, request: Request):
    return inventory.get_recall(_db(request), recall_ref)


@router.post("/admin/recalls/{recall_ref}/contact")
@router.post("/admin/recalls/{recall_ref}/contacted", include_in_schema=False)
@router.post("/admin/recalls/{recall_ref}/mark-contacted", include_in_schema=False)
@router.post("/admin/recalls/{recall_ref}", include_in_schema=False)
@router.patch("/admin/recalls/{recall_ref}", include_in_schema=False)
@router.put("/admin/recalls/{recall_ref}/contact", include_in_schema=False)
@router.post("/admin/recall-cases/{recall_ref}/contact", include_in_schema=False)
def contact_recall(
    recall_ref: str,
    request: Request,
    event_id: str | None = None,
    quality_event_id: str | None = None,
    body: dict[str, Any] | None = None,
):
    data = _body(body)
    return inventory.contact_recall_from_payload(
        _db(request),
        data,
        recall_ref=recall_ref,
        event_ref=event_id or quality_event_id or inventory._first(
            data, "event_id", "quality_event_id", "event_no"
        ),
    )


@router.post("/admin/quality-events/{event_ref}/orders/{order_ref}/contact")
@router.post("/admin/quality-events/{event_ref}/recalls/{order_ref}/contact",
             include_in_schema=False)
def contact_event_order(
    event_ref: str, order_ref: str, request: Request, body: dict[str, Any] | None = None
):
    return inventory.contact_recall_from_payload(
        _db(request), body, event_ref=event_ref, order_ref=order_ref
    )


@router.post("/admin/quality-events/{event_ref}/contact")
def contact_event_by_body(
    event_ref: str, request: Request, body: dict[str, Any] | None = None
):
    data = _body(body)
    order_ref = inventory._first(
        data, "order_id", "outbound_order_id", "order_no"
    )
    recall_ref = inventory._first(data, "recall_id", "recall_case_id")
    if order_ref is None and recall_ref is not None:
        return inventory.contact_recall_from_payload(
            _db(request), data, recall_ref=recall_ref, event_ref=event_ref
        )
    return inventory.contact_recall_from_payload(
        _db(request), data, event_ref=event_ref, order_ref=order_ref
    )


@router.post("/admin/orders/{order_ref}/contact")
@router.post("/admin/outbound-orders/{order_ref}/contact", include_in_schema=False)
def contact_order(
    order_ref: str,
    request: Request,
    event_id: str | None = None,
    quality_event_id: str | None = None,
    body: dict[str, Any] | None = None,
):
    data = _body(body)
    body_event = inventory._first(data, "event_id", "quality_event_id", "event_no")
    return inventory.contact_recall_from_payload(
        _db(request),
        data,
        event_ref=event_id or quality_event_id or body_event,
        order_ref=order_ref,
    )


# ---------------------------------------------------------------------------
# 额外命名兼容别名（不进入 OpenAPI，避免文档被重复路径淹没）
# ---------------------------------------------------------------------------
_EXTRA_BATCH_PREFIXES = (
    "/admin/batch",
    "/admin/lot",
    "/admin/stock-batches",
    "/admin/inbound-batches",
    "/admin/inbound_batches",
    "/admin/stock/batches",
    "/admin/inventory/lots",
    "/admin/produce/batches",
)
for _prefix in _EXTRA_BATCH_PREFIXES:
    router.add_api_route(_prefix, create_batch, methods=["POST"], status_code=201,
                         include_in_schema=False)
    router.add_api_route(_prefix, list_batches, methods=["GET"], include_in_schema=False)
    router.add_api_route(f"{_prefix}/split", compose_batches, methods=["POST"],
                         status_code=201, include_in_schema=False)
    router.add_api_route(f"{_prefix}/merge", compose_batches, methods=["POST"],
                         status_code=201, include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{batch_ref}}", get_batch, methods=["GET"],
                         include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{batch_ref}}/lineage", get_batch_lineage,
                         methods=["GET"], include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{batch_ref}}/split", split_named_batch,
                         methods=["POST"], status_code=201, include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{batch_ref}}/quality-events",
                         create_batch_quality_event, methods=["POST"],
                         status_code=201, include_in_schema=False)

_EXTRA_ORDER_PREFIXES = (
    "/admin/outbound",
    "/admin/dispatch",
    "/admin/deliveries",
    "/admin/shipment-orders",
    "/admin/shipment_orders",
    "/admin/outbound_orders",
    "/admin/dispatch-orders",
    "/admin/dispatch_orders",
    "/admin/delivery_orders",
    "/admin/inventory/shipments",
    "/admin/inventory/outbound",
    "/admin/stock/orders",
)
for _prefix in _EXTRA_ORDER_PREFIXES:
    router.add_api_route(_prefix, create_order, methods=["POST"], status_code=201,
                         include_in_schema=False)
    router.add_api_route(_prefix, list_orders, methods=["GET"], include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{order_ref}}", get_order, methods=["GET"],
                         include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{order_ref}}", replace_order_by_body,
                         methods=["PUT", "PATCH"], include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{order_ref}}/allocations",
                         _save_allocations_route, methods=["PUT", "PATCH"],
                         include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{order_ref}}/allocations", add_allocation,
                         methods=["POST"], include_in_schema=False)
    for _suffix, _endpoint, _methods in (
        ("/prepare", prepare_order, ["POST"]),
        ("/reserve", prepare_order, ["POST"]),
        ("/stock", prepare_order, ["POST"]),
        ("/allocate", allocate_order, ["POST"]),
        ("/deliver", deliver_order, ["POST"]),
        ("/confirm", deliver_order, ["POST"]),
        ("/confirm-delivery", deliver_order, ["POST"]),
        ("/ship", deliver_order, ["POST"]),
        ("/cancel", cancel_order, ["POST"]),
        ("/contact", contact_order, ["POST"]),
    ):
        router.add_api_route(
            f"{_prefix}/{{order_ref}}{_suffix}",
            _endpoint,
            methods=_methods,
            include_in_schema=False,
        )

_EXTRA_EVENT_PREFIXES = (
    "/admin/issues",
    "/admin/quality/issues",
    "/admin/quality_events",
    "/admin/quality-incidents",
    "/admin/quality_incidents",
    "/admin/quarantine-events",
    "/admin/quarantine_events",
    "/admin/quarantine",
    "/admin/quarantines",
)
for _prefix in _EXTRA_EVENT_PREFIXES:
    router.add_api_route(_prefix, create_quality_event, methods=["POST"],
                         status_code=201, include_in_schema=False)
    router.add_api_route(_prefix, list_quality_events, methods=["GET"],
                         include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{event_ref}}", get_quality_event,
                         methods=["GET"], include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{event_ref}}/resolve", resolve_quality_event,
                         methods=["POST"], include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{event_ref}}/release", resolve_quality_event,
                         methods=["POST"], include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{event_ref}}/recalls", list_event_recalls,
                         methods=["GET"], include_in_schema=False)
    router.add_api_route(
        f"{_prefix}/{{event_ref}}/orders/{{order_ref}}/contact",
        contact_event_order,
        methods=["POST"],
        include_in_schema=False,
    )

for _prefix in ("/admin/recall", "/admin/recall-cases"):
    router.add_api_route(_prefix, list_recalls, methods=["GET"], include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{recall_ref}}", get_recall, methods=["GET"],
                         include_in_schema=False)
    router.add_api_route(f"{_prefix}/{{recall_ref}}/contact", contact_recall,
                         methods=["POST"], include_in_schema=False)
