"""Pydantic 请求/响应模型：响应模型即白名单，防止越权字段被带出接口。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Choice = Literal["yes", "no", "abstain"]


class MemberCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    is_admin: bool = False


class MemberOut(BaseModel):
    id: int
    code: str
    name: str
    is_admin: bool
    token: str = Field(description="会员令牌，仅本次创建时返回一次")


class BallotCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class BallotOut(BaseModel):
    id: int
    title: str
    status: str
    created_at: str
    opened_at: Optional[str] = None
    settled_at: Optional[str] = None


class CredentialOut(BaseModel):
    ballot_id: int
    voter_pid: str = Field(description="该资格对应的匿名代号")
    credential: str = Field(description="投票凭证明文，仅本次返回，请妥善保存")
    reissued: bool


class VoteCast(BaseModel):
    credential: str
    choice: Choice


class VoteReceiptOut(BaseModel):
    ballot_id: int
    voter_pid: str
    choice: Choice
    updated_at: str
    changed: bool


class EligibilityAction(BaseModel):
    member_code: str
    action: Literal["suspend", "restore"]


class EligibilityOut(BaseModel):
    ballot_id: int
    member_code: str
    status: Literal["active", "suspended"]
    note: str
    at: str


class RosterRow(BaseModel):
    code: str
    name: str
    status: Literal["active", "suspended"]
    epoch: int
    has_credential: bool


class Results(BaseModel):
    yes: int
    no: int
    abstain: int


class PublicBallotOut(BaseModel):
    ballot_id: int
    title: str
    status: str
    results: Optional[Results] = None
