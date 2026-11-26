"""主服务访问清算模拟端的 HTTP 客户端。

关键约定：
- 只有清算端明确返回 4xx/5xx 业务响应且可读时，才算“明确结果”；
- 连接失败、读超时、响应中断一律抛 :class:`ClearingUnavailable`，
  调用方不得把失联当作付款失败；
- 所有请求都带幂等 ref，重试天然安全，清算端负责去重。
"""
from __future__ import annotations

from typing import Any

import httpx


class ClearingUnavailable(Exception):
    """清算端失联或响应不可读：付款结果未知，不能判失败。"""


class ClearingClient:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self._base_url = base_url.rstrip("/")
        # transport 可注入：测试用 ASGITransport 直连清算应用，无需起端口
        self._client = httpx.Client(base_url=self._base_url, transport=transport, timeout=10.0)

    def close(self) -> None:
        self._client.close()

    @property
    def base_url(self) -> str:
        return self._base_url

    def health(self) -> bool:
        try:
            r = self._client.get("/health")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def submit_payment(self, ref: str, payee: str, amount_cents: int) -> dict[str, Any]:
        """提交付款。明确失败返回含 status 的字典；失联抛 ClearingUnavailable。"""
        try:
            r = self._client.post(
                "/payments",
                json={"ref": ref, "payee": payee, "amount_cents": amount_cents},
            )
        except httpx.HTTPError as exc:  # 连接拒绝、读超时、连接重置等
            raise ClearingUnavailable(str(exc)) from exc
        if r.status_code >= 500:
            # 500 可能正是“扣款后断连”：结果未知，绝不判失败
            raise ClearingUnavailable(f"清算端响应异常 status={r.status_code}")
        if r.status_code >= 400:
            detail = r.text
            try:
                detail = r.json().get("detail", detail)
            except ValueError:
                pass
            # 4xx 是清算端可读的明确拒绝（本模拟端当前不使用，预留语义）
            return {"status": "failed", "detail": f"清算端拒绝: {detail}"}
        return r.json()

    def get_payment(self, ref: str) -> dict[str, Any]:
        try:
            r = self._client.get(f"/payments/{ref}")
        except httpx.HTTPError as exc:
            raise ClearingUnavailable(str(exc)) from exc
        if r.status_code == 404:
            raise ClearingUnavailable("清算端查无此流水（可能尚未受理成功）")
        if r.status_code >= 400:
            raise ClearingUnavailable(f"清算端查询异常 status={r.status_code}")
        return r.json()
