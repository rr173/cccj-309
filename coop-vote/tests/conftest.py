"""pytest 公共夹具：每个测试用独立临时数据库与 TestClient。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import service  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "coopvote_test.db")
    salt = "test-credential-salt"
    app = create_app(db_path=db_path, credential_salt=salt)
    client = TestClient(app)
    # 首位管理员直接在服务层播种（本地演示的 bootstrap 方式）
    admin = service.create_member(db_path, salt, "ADMIN01", "名册管理员", is_admin=True)
    return SimpleNamespace(db=db_path, salt=salt, app=app, client=client, admin=admin)


def create_member(env, code: str, name: str, is_admin: bool = False) -> dict:
    """通过管理员接口创建会员，返回含明文 token 的响应。"""
    r = env.client.post(
        "/admin/members",
        headers={"X-Member-Token": env.admin["token"]},
        json={"code": code, "name": name, "is_admin": is_admin},
    )
    assert r.status_code == 201, r.text
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
