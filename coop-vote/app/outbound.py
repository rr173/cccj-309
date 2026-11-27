"""出库单服务的兼容导入入口。"""
from .inventory import (  # noqa: F401
    add_allocation,
    cancel_order,
    create_outbound_order,
    deliver_order,
    get_order,
    list_orders,
    prepare_order,
    save_allocations,
)
