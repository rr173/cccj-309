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


# ---------------------------------------------------------------- 资金/拨款
class DepositIn(BaseModel):
    amount_cents: int = Field(gt=0, description="入金金额，正整数分")


class PoolOut(BaseModel):
    deposited_cents: int
    reserved_cents: int
    paid_cents: int
    available_cents: int
    updated_at: str


class AllocateProposalIn(BaseModel):
    payee: str = Field(min_length=1, max_length=128, description="固定收款方")
    max_amount_cents: int = Field(gt=0, description="拨款上限，正整数分")


class RevokeProposalIn(BaseModel):
    grant_id: int = Field(gt=0)


class ProposalOut(BaseModel):
    id: int
    ballot_id: int
    kind: Literal["allocate", "revoke"]
    payee: Optional[str] = None
    max_amount_cents: Optional[int] = None
    grant_id: Optional[int] = None
    status: str
    note: Optional[str] = None
    executed_at: Optional[str] = None
    resulting_grant_id: Optional[int] = None


class GrantOut(BaseModel):
    id: int
    ballot_id: int
    proposal_id: int
    payee: str
    status: Literal["active", "revoked"]
    max_amount_cents: int
    reserved_cents: int
    paid_cents: int
    accepted_pending_cents: int
    available_for_payment_cents: int
    effective: bool
    created_at: str
    revoked_at: Optional[str] = None


class PaymentIn(BaseModel):
    request_no: str = Field(min_length=1, max_length=128)
    amount_cents: int = Field(gt=0, description="单笔付款，正整数分")


class PaymentOut(BaseModel):
    id: int
    grant_id: int
    request_no: str
    payee: str
    amount_cents: int
    status: Literal["accepted", "paid", "failed"]
    clearing_ref: str
    detail: Optional[str] = None
    created_at: str
    updated_at: str
