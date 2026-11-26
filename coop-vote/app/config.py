"""本地演示配置：数据库路径与凭证哈希盐值均来自环境变量，带默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    db_path: str
    credential_salt: str
    clearing_base_url: str
    clearing_db_path: str
    reconcile_interval_seconds: float


def get_settings() -> Settings:
    return Settings(
        db_path=os.environ.get(
            "COOPVOTE_DB", str(PROJECT_ROOT / "coopvote.db")
        ),
        credential_salt=os.environ.get(
            "COOPVOTE_CRED_SALT", "local-demo-salt-change-me"
        ),
        # 本地清算模拟端（独立进程、独立持久化数据库）的访问地址
        clearing_base_url=os.environ.get(
            "COOPVOTE_CLEARING_URL", "http://127.0.0.1:8090"
        ),
        clearing_db_path=os.environ.get(
            "COOPVOTE_CLEARING_DB", str(PROJECT_ROOT / "clearing.db")
        ),
        reconcile_interval_seconds=float(
            os.environ.get("COOPVOTE_RECONCILE_INTERVAL", "2.0")
        ),
    )
