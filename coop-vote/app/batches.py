"""批次领域的兼容导入入口；规范实现位于 :mod:`app.inventory`。"""
from __future__ import annotations

from .inventory import *  # noqa: F401,F403
from .inventory import (  # noqa: F401
    add_allocation,
    compose_batches,
    create_batch,
    create_batch_from_payload,
    get_batch,
    list_batches,
    merge_batches,
    save_allocations,
    split_batches,
)
