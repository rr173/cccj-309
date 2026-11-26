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


def get_settings() -> Settings:
    return Settings(
        db_path=os.environ.get(
            "COOPVOTE_DB", str(PROJECT_ROOT / "coopvote.db")
        ),
        credential_salt=os.environ.get(
            "COOPVOTE_CRED_SALT", "local-demo-salt-change-me"
        ),
    )
