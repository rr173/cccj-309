"""召回服务的兼容导入入口；规范实现位于 :mod:`app.inventory`。"""
from .inventory import (  # noqa: F401
    contact_recall,
    contact_recall_from_payload,
    create_quality_event,
    get_quality_event,
    get_recall,
    list_recalls,
    resolve_quality_event,
)
