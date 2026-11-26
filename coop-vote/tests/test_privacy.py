"""隐私边界测试：各业务接口只在各自权限范围内读数据，互不跨读。"""
from __future__ import annotations

from app.database import read

from .conftest import create_member, issue, make_open_ballot, vote


def test_roster_admin_sees_eligibility_and_credential_flag_but_never_choices(env):
    m1 = create_member(env, "M01", "张三")
    m2 = create_member(env, "M02", "李四")
    ballot_id = make_open_ballot(env)
    c1 = issue(env, ballot_id, m1)
    vote(env, ballot_id, c1["credential"], "yes")
    # M02 只领票未投票
    issue(env, ballot_id, m2)

    r = env.client.get(
        f"/admin/ballots/{ballot_id}/eligibility",
        headers={"X-Member-Token": env.admin["token"]},
    )
    assert r.status_code == 200
    rows = r.json()
    by_code = {row["code"]: row for row in rows}
    assert by_code["M01"]["has_credential"] is True
    assert by_code["M02"]["has_credential"] is True
    # 白名单字段：没有 choice/vote/voter_pid 等任何选择相关字段
    for row in rows:
        assert set(row.keys()) == {"code", "name", "status", "epoch", "has_credential"}
        assert "choice" not in row and "vote" not in row and "voter_pid" not in row

    # 名册里无法区分“已投票”和“仅领票”
    assert "voted" not in rows[0]


def test_vote_endpoint_does_not_reveal_identity(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    c = issue(env, ballot_id, m)
    r = vote(env, ballot_id, c["credential"], "no")
    assert r.status_code == 200
    body = r.json()
    # 响应只含匿名代号与选择，不含任何会员身份字段
    assert set(body.keys()) == {"ballot_id", "voter_pid", "choice", "updated_at", "changed"}
    assert "M01" not in str(body)
    assert "张三" not in str(body)
    assert "member" not in str(body).lower()

    # 投票接口不接受也不识别会员令牌
    r2 = env.client.post(
        f"/ballots/{ballot_id}/vote",
        headers={"X-Member-Token": m["token"]},
        json={"credential": c["credential"], "choice": "no"},
    )
    assert r2.status_code == 200  # 令牌头被直接忽略，凭证独立生效


def test_member_token_cannot_be_used_as_vote_credential(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    issue(env, ballot_id, m)
    r = vote(env, ballot_id, m["token"], "yes")  # 拿会员令牌冒充凭证
    assert r.status_code == 401


def test_credentials_are_stored_only_hashed_and_no_plaintext_at_rest(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    issued = issue(env, ballot_id, m)
    with read(env.db) as conn:
        raw = "\n".join(
            str(dict(r)) for r in conn.execute("SELECT * FROM credentials").fetchall()
        )
        assert issued["credential"] not in raw  # 明文凭证不落库
        assert len(conn.execute("SELECT token_hash FROM credentials").fetchall()) == 1
        # 会员表同样只有哈希
        member_raw = str(
            dict(conn.execute("SELECT * FROM members WHERE code='M01'").fetchone())
        )
        assert m["token"] not in member_raw


def test_public_endpoint_only_exposes_totals_after_settlement(env):
    members = [create_member(env, f"M{i:02d}", f"会员{i}") for i in range(1, 4)]
    ballot_id = make_open_ballot(env)
    for m, choice in zip(members, ["yes", "no", "yes"]):
        c = issue(env, ballot_id, m)
        vote(env, ballot_id, c["credential"], choice)

    # 开放期公开端没有任何计数
    before = env.client.get(f"/public/ballots/{ballot_id}").json()
    assert before["status"] == "open"
    assert before["results"] is None

    env.client.post(
        f"/admin/ballots/{ballot_id}/settle",
        headers={"X-Member-Token": env.admin["token"]},
    )
    after = env.client.get(f"/public/ballots/{ballot_id}").json()
    assert after["status"] == "settled"
    assert after["results"] == {"yes": 2, "no": 1, "abstain": 0}
    # 公开响应没有逐票、代号、时间或顺序信息
    assert "votes" not in after and "ballots_detail" not in after
    assert set(after.keys()) == {"ballot_id", "title", "status", "results"}


def test_non_admin_cannot_access_admin_roster_or_ballot_ops(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    headers = {"X-Member-Token": m["token"]}
    assert env.client.get(f"/admin/ballots/{ballot_id}/eligibility", headers=headers).status_code == 403
    assert env.client.post(
        f"/admin/ballots/{ballot_id}/settle", headers=headers
    ).status_code == 403
    assert env.client.post(
        f"/admin/ballots/{ballot_id}/eligibility",
        headers=headers,
        json={"member_code": "M01", "action": "suspend"},
    ).status_code == 403


def test_missing_or_bad_member_token_is_rejected(env):
    ballot_id = make_open_ballot(env)
    assert env.client.post(f"/me/ballots/{ballot_id}/credential").status_code == 401
    assert env.client.post(
        f"/me/ballots/{ballot_id}/credential",
        headers={"X-Member-Token": "not-a-real-token"},
    ).status_code == 401


def test_no_endpoint_returns_any_vote_choice_for_admin(env):
    """管理员可核对领票/资格状态，但系统不提供读取任何人选择的接口。"""
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    c = issue(env, ballot_id, m)
    vote(env, ballot_id, c["credential"], "abstain")

    admin_headers = {"X-Member-Token": env.admin["token"]}
    paths = [
        ("GET", f"/admin/ballots/{ballot_id}/eligibility"),
        ("GET", f"/public/ballots/{ballot_id}"),
    ]
    for method, path in paths:
        r = env.client.request(method, path, headers=admin_headers)
        assert "abstain" not in r.text  # 具体选择不出现在任何管理/公开响应中
