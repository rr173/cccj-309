"""业务规则测试：冻结范围、改投、重领、停权/恢复、结算固化。"""
from __future__ import annotations

from app import service
from app.database import read

from .conftest import create_member, issue, make_open_ballot, vote


def test_open_freezes_member_scope_and_late_joiners_are_excluded(env):
    m1 = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env, "年度预算表决")
    # 开启后新增会员不在冻结范围内
    m2 = create_member(env, "M02", "李四")

    c1 = issue(env, ballot_id, m1)
    r = env.client.post(
        f"/me/ballots/{ballot_id}/credential",
        headers={"X-Member-Token": m2["token"]},
    )
    assert r.status_code == 403
    assert "冻结会员范围" in r.json()["detail"]

    r = vote(env, ballot_id, c1["credential"], "yes")
    assert r.status_code == 200
    assert r.json()["choice"] == "yes"


def test_reissue_keeps_single_eligibility_and_invalidates_old_credential(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)

    first = issue(env, ballot_id, m)
    second = issue(env, ballot_id, m)
    assert first["reissued"] is False       # 首次领取
    assert second["reissued"] is True       # 重领
    assert first["voter_pid"] == second["voter_pid"]  # 匿名代号不变 → 同一计票资格
    assert first["credential"] != second["credential"]

    # 旧凭证立即作废，不能产生第二条投票
    r = vote(env, ballot_id, first["credential"], "yes")
    assert r.status_code == 401
    r = vote(env, ballot_id, second["credential"], "no")
    assert r.status_code == 200

    with read(env.db) as conn:
        vote_rows = conn.execute("SELECT COUNT(*) AS n FROM votes").fetchone()
        # 重领不新增资格行：M01 始终只有一条资格
        elig_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM eligibilities e "
            "JOIN members m ON m.id=e.member_id WHERE m.code='M01'"
        ).fetchone()
        active_creds = conn.execute(
            "SELECT COUNT(*) AS n FROM credentials WHERE active=1"
        ).fetchone()
        assert vote_rows["n"] == 1
        assert elig_rows["n"] == 1
        assert active_creds["n"] == 1  # 旧凭证已失效，只剩新凭证


def test_revote_overwrites_and_only_last_choice_counts(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    c = issue(env, ballot_id, m)

    for choice in ("yes", "abstain", "no"):
        r = vote(env, ballot_id, c["credential"], choice)
        assert r.status_code == 200
    with read(env.db) as conn:
        rows = conn.execute("SELECT choice FROM votes").fetchall()
        assert len(rows) == 1
        assert rows[0]["choice"] == "no"  # 截止只计最后一次


def test_suspension_blocks_voting_and_excludes_existing_vote(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    c = issue(env, ballot_id, m)
    assert vote(env, ballot_id, c["credential"], "yes").status_code == 200

    r = env.client.post(
        f"/admin/ballots/{ballot_id}/eligibility",
        headers={"X-Member-Token": env.admin["token"]},
        json={"member_code": "M01", "action": "suspend"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "suspended"

    # 停权期间不能领新凭证、不能投票
    r = env.client.post(
        f"/me/ballots/{ballot_id}/credential",
        headers={"X-Member-Token": m["token"]},
    )
    assert r.status_code == 403
    r = vote(env, ballot_id, c["credential"], "no")
    assert r.status_code == 403

    # 停权前的票已暂不计入（底层票已移除）
    with read(env.db) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM votes").fetchone()["n"] == 0

    # 结算时停权会员不产生任何计数
    res = env.client.post(
        f"/admin/ballots/{ballot_id}/settle",
        headers={"X-Member-Token": env.admin["token"]},
    ).json()
    assert res["results"] == {"yes": 0, "no": 0, "abstain": 0}


def test_restore_does_not_revive_old_vote_and_member_must_revote(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    c = issue(env, ballot_id, m)
    vote(env, ballot_id, c["credential"], "yes")

    service.set_eligibility(env.db, ballot_id, "M01", suspend=True)
    out = service.set_eligibility(env.db, ballot_id, "M01", suspend=False)
    assert out["status"] == "active"

    # 旧票不恢复
    with read(env.db) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM votes").fetchone()["n"] == 0

    # 恢复后可重新领凭证、重新投票
    c2 = issue(env, ballot_id, m)
    assert c2["voter_pid"] == c["voter_pid"]  # 仍是同一资格/代号
    r = vote(env, ballot_id, c2["credential"], "abstain")
    assert r.status_code == 200

    res = env.client.post(
        f"/admin/ballots/{ballot_id}/settle",
        headers={"X-Member-Token": env.admin["token"]},
    ).json()
    assert res["results"] == {"yes": 0, "no": 0, "abstain": 1}


def test_full_workflow_counts_three_choices_correctly(env):
    members = [create_member(env, f"M{i:02d}", f"会员{i}") for i in range(1, 6)]
    ballot_id = make_open_ballot(env)
    choices = ["yes", "yes", "no", "abstain", "yes"]
    for m, choice in zip(members, choices):
        c = issue(env, ballot_id, m)
        assert vote(env, ballot_id, c["credential"], choice).status_code == 200

    # 改投后再结算，确认以最后选择为准
    c = issue(env, ballot_id, members[0])
    vote(env, ballot_id, c["credential"], "no")

    res = env.client.post(
        f"/admin/ballots/{ballot_id}/settle",
        headers={"X-Member-Token": env.admin["token"]},
    ).json()
    assert res["results"] == {"yes": 2, "no": 2, "abstain": 1}


def test_settled_result_is_frozen_against_later_eligibility_changes(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    c = issue(env, ballot_id, m)
    vote(env, ballot_id, c["credential"], "yes")
    env.client.post(
        f"/admin/ballots/{ballot_id}/settle",
        headers={"X-Member-Token": env.admin["token"]},
    )

    # 结算后停权/恢复、投票、领凭证全部被拒
    r = env.client.post(
        f"/admin/ballots/{ballot_id}/eligibility",
        headers={"X-Member-Token": env.admin["token"]},
        json={"member_code": "M01", "action": "suspend"},
    )
    assert r.status_code == 409
    assert vote(env, ballot_id, c["credential"], "no").status_code == 409
    r = env.client.post(
        f"/me/ballots/{ballot_id}/credential",
        headers={"X-Member-Token": m["token"]},
    )
    assert r.status_code == 409

    # 公开结果不变
    res = env.client.get(f"/public/ballots/{ballot_id}").json()
    assert res["status"] == "settled"
    assert res["results"] == {"yes": 1, "no": 0, "abstain": 0}


def test_draft_ballot_rejects_credentials_and_votes(env):
    m = create_member(env, "M01", "张三")
    r = env.client.post(
        "/admin/ballots",
        headers={"X-Member-Token": env.admin["token"]},
        json={"title": "未开启表决"},
    )
    ballot_id = r.json()["id"]
    r = env.client.post(
        f"/me/ballots/{ballot_id}/credential",
        headers={"X-Member-Token": m["token"]},
    )
    assert r.status_code == 409
    r = vote(env, ballot_id, "bogus-credential", "yes")
    assert r.status_code == 401


def test_suspend_restore_are_idempotent(env):
    create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    first = service.set_eligibility(env.db, ballot_id, "M01", suspend=True)
    second = service.set_eligibility(env.db, ballot_id, "M01", suspend=True)
    assert first["status"] == second["status"] == "suspended"
    out = service.set_eligibility(env.db, ballot_id, "M01", suspend=False)
    assert out["status"] == "active"
