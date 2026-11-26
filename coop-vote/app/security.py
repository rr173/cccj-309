"""随机令牌生成、哈希与请求鉴权依赖。

- 会员令牌（X-Member-Token）与投票凭证使用不同前缀的哈希，互不通用。
- 数据库只保存哈希值，明文令牌仅在签发时返回一次。
"""
from __future__ import annotations

import hashlib
import secrets

from fastapi import Header, Request

from .database import read


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _digest(namespace: str, salt: str, token: str) -> str:
    return hashlib.sha256(f"{namespace}:{salt}:{token}".encode()).hexdigest()


def hash_member_token(token: str, salt: str) -> str:
    return _digest("member-token", salt, token)


def hash_credential(token: str, salt: str) -> str:
    return _digest("vote-credential", salt, token)


def authenticate_member(request: Request, x_member_token: str = Header(default=None)):
    """解析会员令牌，返回 members 行；失败返回 None（由路由决定 401）。"""
    if not x_member_token:
        return None
    settings = request.app.state.settings
    with read(settings.db_path) as conn:
        return conn.execute(
            "SELECT * FROM members WHERE token_hash=?",
            (hash_member_token(x_member_token, settings.credential_salt),),
        ).fetchone()
