"""质量事件/隔离服务的兼容导入入口。"""
from .inventory import (  # noqa: F401
    create_quality_event,
    get_quality_event,
    list_quality_events,
    resolve_quality_event,
)
