"""pytest 公共夹具：每个测试用独立临时数据库与 TestClient。

主服务与清算模拟端各用一个独立 SQLite 文件；测试中清算客户端通过
自定义同步 ASGI 传输直连清算应用，无需监听端口。
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import service  # noqa: E402
from app.clearing.app import create_clearing_app  # noqa: E402
from app.clearing_client import ClearingClient  # noqa: E402
from app.database import read  # noqa: E402
from app.main import create_app  # noqa: E402


class SyncASGITransport(httpx.BaseTransport):
    """把 ASGI 应用桥接到 httpx 同步传输的适配器。

    在专用线程里跑一个常驻事件循环，避免与 TestClient 自身的循环嵌套冲突。
    ``drop_payments=True`` 时，对 ``POST /payments`` 的请求在清算应用
    抛出穿透异常（扣款已提交、响应断开）时转成 :class:`httpx.ReadError`，
    真实模拟“已扣款后失联”；其它请求照常返回。
    """

    def __init__(self, app, drop_payments: bool = False):
        self.app = app
        self.drop_payments = drop_payments
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="asgi-bridge", daemon=True
        )
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        is_payment_post = request.method == "POST" and request.url.path == "/payments"
        body_chunks = [request.content]

        async def receive():
            if body_chunks:
                return {"type": "http.request", "body": body_chunks.pop(0)}
            return {"type": "http.request", "body": b"", "more_body": False}

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        async def run():
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": request.method,
                "scheme": request.url.scheme,
                "path": request.url.raw_path.decode("ascii"),
                "raw_path": request.url.raw_path,
                "query_string": request.url.query,
                "headers": [(k.lower(), v) for k, v in request.headers.raw],
                "client": ("testclient", 123),
                "server": ("testserver", 80),
            }
            await self.app(scope, receive, send)

        future = asyncio.run_coroutine_threadsafe(run(), self._loop)
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            if self.drop_payments and is_payment_post:
                raise httpx.ReadError(f"模拟连接断开（付款可能已扣款）: {exc}") from exc
            raise

        status_code, headers, body = 500, [], b""
        for m in messages:
            if m["type"] == "http.response.start":
                status_code = m["status"]
                headers = m.get("headers", [])
            elif m["type"] == "http.response.body":
                body += m.get("body", b"")
        return httpx.Response(
            status_code, headers=dict(headers), content=body, request=request
        )


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "coopvote_test.db")
    clearing_db_path = str(tmp_path / "clearing_test.db")
    salt = "test-credential-salt"

    clearing_app = create_clearing_app(clearing_db_path)
    clearing_client = ClearingClient(
        base_url="http://clearing-test",
        transport=SyncASGITransport(clearing_app, drop_payments=True),
    )

    app = create_app(
        db_path=db_path,
        credential_salt=salt,
        clearing_client=clearing_client,
        start_reconciler=False,  # 测试内手动对账，避免后台线程干扰断言
    )
    client = TestClient(app)
    # 首位管理员直接在服务层播种（本地演示的 bootstrap 方式）
    admin = service.create_member(db_path, salt, "ADMIN01", "名册管理员", is_admin=True)
    ns = SimpleNamespace(
        db=db_path,
        clearing_db=clearing_db_path,
        salt=salt,
        app=app,
        client=client,
        clearing_app=clearing_app,
        clearing_client=clearing_client,
        admin=admin,
        tokens={admin["code"]: admin["token"]},
    )
    return ns


def create_member(env, code: str, name: str, is_admin: bool = False) -> dict:
    """通过管理员接口创建会员，返回含明文 token 的响应，并登记到 env.tokens。"""
    r = env.client.post(
        "/admin/members",
        headers={"X-Member-Token": env.admin["token"]},
        json={"code": code, "name": name, "is_admin": is_admin},
    )
    assert r.status_code == 201, r.text
    env.tokens[code] = r.json()["token"]
    return r.json()


def make_open_ballot(env, title: str = "示例表决") -> int:
    r = env.client.post(
        "/admin/ballots",
        headers={"X-Member-Token": env.admin["token"]},
        json={"title": title},
    )
    assert r.status_code == 201, r.text
    ballot_id = r.json()["id"]
    r = env.client.post(
        f"/admin/ballots/{ballot_id}/open",
        headers={"X-Member-Token": env.admin["token"]},
    )
    assert r.status_code == 200, r.text
    return ballot_id


def make_allocate_ballot(
    env,
    payee: str,
    max_amount_cents: int,
    title: str = "拨款表决",
    voters: int = 3,
) -> int:
    """建 draft 表决、绑定拨款提案；先建好表决会员再开启（冻结范围）。

    返回已开启的 ballot_id；调用方可直接用 approve_ballot 结算。
    """
    r = env.client.post(
        "/admin/ballots",
        headers={"X-Member-Token": env.admin["token"]},
        json={"title": title},
    )
    assert r.status_code == 201, r.text
    ballot_id = r.json()["id"]
    r = env.client.post(
        f"/admin/ballots/{ballot_id}/proposals/allocate",
        headers={"X-Member-Token": env.admin["token"]},
        json={"payee": payee, "max_amount_cents": max_amount_cents},
    )
    assert r.status_code == 201, r.text
    for i in range(voters):
        create_member(env, f"V{ballot_id}-{i:02d}", f"表决人{ballot_id}-{i}")
    r = env.client.post(
        f"/admin/ballots/{ballot_id}/open",
        headers={"X-Member-Token": env.admin["token"]},
    )
    assert r.status_code == 200, r.text
    return ballot_id


def ballot_member_codes(env, ballot_id: int) -> list[str]:
    """取某次表决冻结范围内的会员编号（按会员 id 排序）。"""
    with read(env.db) as conn:
        rows = conn.execute(
            "SELECT m.code AS code FROM eligibilities e "
            "JOIN members m ON m.id = e.member_id "
            "WHERE e.ballot_id=? ORDER BY m.id",
            (ballot_id,),
        ).fetchall()
    return [r["code"] for r in rows]


def approve_ballot(env, ballot_id: int, voters: int | None = None) -> None:
    """让冻结范围内的会员全部投赞成（yes > no），然后结算。

    ``voters`` 给定时先建会员再开启；否则假定表决已开启、范围内已有会员。
    """
    if voters is not None:
        for i in range(voters):
            create_member(env, f"V{ballot_id}-{i:02d}", f"表决人{ballot_id}-{i}")
        r = env.client.post(
            f"/admin/ballots/{ballot_id}/open",
            headers={"X-Member-Token": env.admin["token"]},
        )
        assert r.status_code == 200, r.text

    for code in ballot_member_codes(env, ballot_id):
        c = env.client.post(
            f"/me/ballots/{ballot_id}/credential",
            headers={"X-Member-Token": env.tokens[code]},
        ).json()["credential"]
        r = env.client.post(
            f"/ballots/{ballot_id}/vote",
            json={"credential": c, "choice": "yes"},
        )
        assert r.status_code == 200, r.text
    r = env.client.post(
        f"/admin/ballots/{ballot_id}/settle",
        headers={"X-Member-Token": env.admin["token"]},
    )
    assert r.status_code == 200, r.text


def issue(env, ballot_id: int, member: dict) -> dict:
    r = env.client.post(
        f"/me/ballots/{ballot_id}/credential",
        headers={"X-Member-Token": member["token"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def vote(env, ballot_id: int, credential: str, choice: str):
    return env.client.post(
        f"/ballots/{ballot_id}/vote",
        json={"credential": credential, "choice": choice},
    )
