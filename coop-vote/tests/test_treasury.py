"""拨款提案与资金池规则测试。

覆盖：提案绑定约束、开放后冻结、赞成不大于反对不通过、批准即全额保留、
余额不足不生效且不自动恢复、撤销退款语义、资金池账目恒等式。
"""
from __future__ import annotations

from app.database import read

from .conftest import approve_ballot, create_member, issue, vote


ADMIN = lambda env: {"X-Member-Token": env.admin["token"]}


def _pool(env):
    return env.client.get("/admin/treasury/pool", headers=ADMIN(env)).json()


def _deposit(env, cents: int):
    r = env.client.post(
        "/admin/treasury/pool/deposit", headers=ADMIN(env), json={"amount_cents": cents}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _assert_ledger_invariant(env):
    """资金池恒等式：available + reserved + paid == deposited，且非负。"""
    with read(env.db) as conn:
        pool = conn.execute("SELECT * FROM fund_pool WHERE id=1").fetchone()
        assert pool["deposited_cents"] >= 0
        assert pool["reserved_cents"] >= 0
        assert pool["paid_cents"] >= 0
        view = _pool(env)
        assert (
            view["available_cents"] + view["reserved_cents"] + view["paid_cents"]
            == pool["deposited_cents"]
        )
        assert view["available_cents"] == (
            pool["deposited_cents"] - pool["reserved_cents"] - pool["paid_cents"]
        )
        # 资金池保留额 = 所有拨款保留额之和（撤销后未占用部分为 0）
        grant_reserved = conn.execute(
            "SELECT COALESCE(SUM(reserved_cents),0) AS n FROM grants"
        ).fetchone()["n"]
        assert grant_reserved == pool["reserved_cents"]
        # 资金池已付额 = 所有拨款已付额之和
        grant_paid = conn.execute(
            "SELECT COALESCE(SUM(paid_cents),0) AS n FROM grants"
        ).fetchone()["n"]
        assert grant_paid == pool["paid_cents"]


def test_deposit_and_pool_view(env):
    out = _deposit(env, 5000)
    assert out["deposited_cents"] == 5000
    assert out["reserved_cents"] == 0
    assert out["paid_cents"] == 0
    assert out["available_cents"] == 5000
    r = env.client.post(
        "/admin/treasury/pool/deposit", headers=ADMIN(env), json={"amount_cents": 0}
    )
    assert r.status_code == 422
    r = env.client.post(
        "/admin/treasury/pool/deposit", headers=ADMIN(env), json={"amount_cents": -1}
    )
    assert r.status_code == 422
    _assert_ledger_invariant(env)


def test_proposal_rejects_non_positive_amount_and_empty_payee(env):
    r = env.client.post("/admin/ballots", headers=ADMIN(env), json={"title": "案"})
    bid = r.json()["id"]
    r = env.client.post(
        f"/admin/ballots/{bid}/proposals/allocate",
        headers=ADMIN(env),
        json={"payee": "供应商A", "max_amount_cents": 0},
    )
    assert r.status_code == 422
    r = env.client.post(
        f"/admin/ballots/{bid}/proposals/allocate",
        headers=ADMIN(env),
        json={"payee": "   ", "max_amount_cents": 100},
    )
    assert r.status_code == 422


def test_one_ballot_binds_at_most_one_proposal_and_open_freezes_it(env):
    r = env.client.post("/admin/ballots", headers=ADMIN(env), json={"title": "案"})
    bid = r.json()["id"]
    r = env.client.post(
        f"/admin/ballots/{bid}/proposals/allocate",
        headers=ADMIN(env),
        json={"payee": "供应商A", "max_amount_cents": 1000},
    )
    assert r.status_code == 201
    # 第二提案被拒
    r = env.client.post(
        f"/admin/ballots/{bid}/proposals/allocate",
        headers=ADMIN(env),
        json={"payee": "供应商B", "max_amount_cents": 2000},
    )
    assert r.status_code == 409
    # 开放后不得新增或更改提案
    assert env.client.post(f"/admin/ballots/{bid}/open", headers=ADMIN(env)).status_code == 200
    r = env.client.post(
        f"/admin/ballots/{bid}/proposals/allocate",
        headers=ADMIN(env),
        json={"payee": "供应商C", "max_amount_cents": 500},
    )
    assert r.status_code == 409


def test_approved_allocate_reserves_full_amount_and_grant_is_effective(env):
    _deposit(env, 10000)
    bid = make_open_ballot_with_proposal(env, "供应商A", 3000)
    approve_ballot(env, bid)

    pool = _pool(env)
    assert pool["reserved_cents"] == 3000
    assert pool["available_cents"] == 7000

    grants = env.client.get("/admin/treasury/grants", headers=ADMIN(env)).json()
    assert len(grants) == 1
    g = grants[0]
    assert g["payee"] == "供应商A"
    assert g["max_amount_cents"] == 3000
    assert g["status"] == "active"
    assert g["effective"] is True
    assert g["available_for_payment_cents"] == 3000

    proposal = env.client.get(f"/admin/ballots/{bid}/proposal", headers=ADMIN(env)).json()
    assert proposal["status"] == "approved"
    assert proposal["resulting_grant_id"] == g["id"]
    _assert_ledger_invariant(env)


def test_rejected_proposal_when_yes_not_more_than_no(env):
    _deposit(env, 10000)
    # 先建 draft 表决与提案、先建会员，再开启
    r = env.client.post("/admin/ballots", headers=ADMIN(env), json={"title": "案"})
    bid = r.json()["id"]
    env.client.post(
        f"/admin/ballots/{bid}/proposals/allocate",
        headers=ADMIN(env),
        json={"payee": "供应商A", "max_amount_cents": 3000},
    )
    members = [create_member(env, f"V{bid}-{i:02d}", f"人{i}") for i in range(3)]
    env.client.post(f"/admin/ballots/{bid}/open", headers=ADMIN(env))
    # 1 赞成 vs 2 反对（平票也不通过，这里明确反对更多）
    choices = ["yes", "no", "no"]
    for m, ch in zip(members, choices):
        c = issue(env, bid, m)["credential"]
        assert vote(env, bid, c, ch).status_code == 200
    res = env.client.post(f"/admin/ballots/{bid}/settle", headers=ADMIN(env)).json()
    assert res["results"] == {"yes": 1, "no": 2, "abstain": 0}

    proposal = env.client.get(f"/admin/ballots/{bid}/proposal", headers=ADMIN(env)).json()
    assert proposal["status"] == "rejected"
    assert env.client.get("/admin/treasury/grants", headers=ADMIN(env)).json() == []
    pool = _pool(env)
    assert pool["reserved_cents"] == 0 and pool["available_cents"] == 10000
    _assert_ledger_invariant(env)


def test_tie_vote_does_not_pass(env):
    _deposit(env, 10000)
    r = env.client.post("/admin/ballots", headers=ADMIN(env), json={"title": "案"})
    bid = r.json()["id"]
    env.client.post(
        f"/admin/ballots/{bid}/proposals/allocate",
        headers=ADMIN(env),
        json={"payee": "供应商A", "max_amount_cents": 1000},
    )
    members = [create_member(env, f"V{bid}-{i:02d}", f"人{i}") for i in range(2)]
    env.client.post(f"/admin/ballots/{bid}/open", headers=ADMIN(env))
    for m, ch in zip(members, ["yes", "no"]):
        c = issue(env, bid, m)["credential"]
        vote(env, bid, c, ch)
    env.client.post(f"/admin/ballots/{bid}/settle", headers=ADMIN(env))
    proposal = env.client.get(f"/admin/ballots/{bid}/proposal", headers=ADMIN(env)).json()
    assert proposal["status"] == "rejected"
    assert _pool(env)["reserved_cents"] == 0


def test_insufficient_funds_grant_never_takes_effect(env):
    _deposit(env, 1000)  # 资金池只有 1000 分
    bid = make_open_ballot_with_proposal(env, "供应商A", 5000)
    approve_ballot(env, bid)

    # 表决总数照常公布（冻结范围含管理员，3 名表决会员 + 管理员共 4 张赞成）
    public = env.client.get(f"/public/ballots/{bid}").json()
    assert public["results"]["yes"] == 4
    # 拨款不生效
    proposal = env.client.get(f"/admin/ballots/{bid}/proposal", headers=ADMIN(env)).json()
    assert proposal["status"] == "insufficient_funds"
    assert env.client.get("/admin/treasury/grants", headers=ADMIN(env)).json() == []
    pool = _pool(env)
    assert pool["reserved_cents"] == 0 and pool["available_cents"] == 1000

    # 后来补足余额，拨款也不会自行生效
    _deposit(env, 10000)
    assert env.client.get("/admin/treasury/grants", headers=ADMIN(env)).json() == []
    proposal = env.client.get(f"/admin/ballots/{bid}/proposal", headers=ADMIN(env)).json()
    assert proposal["status"] == "insufficient_funds"
    _assert_ledger_invariant(env)


def test_two_proposals_compete_for_same_pool_only_first_affordable_wins(env):
    _deposit(env, 3000)
    bid1 = make_open_ballot_with_proposal(env, "供应商A", 2000)
    approve_ballot(env, bid1)
    assert _pool(env)["available_cents"] == 1000

    bid2 = make_open_ballot_with_proposal(env, "供应商B", 2000)  # 只差 1000
    approve_ballot(env, bid2)
    p2 = env.client.get(f"/admin/ballots/{bid2}/proposal", headers=ADMIN(env)).json()
    assert p2["status"] == "insufficient_funds"
    grants = env.client.get("/admin/treasury/grants", headers=ADMIN(env)).json()
    assert len(grants) == 1 and grants[0]["payee"] == "供应商A"
    _assert_ledger_invariant(env)


def test_revoke_refunds_unused_reservation_but_keeps_paid_money(env):
    _deposit(env, 10000)
    bid = make_open_ballot_with_proposal(env, "供应商A", 3000)
    approve_ballot(env, bid)
    grant = env.client.get("/admin/treasury/grants", headers=ADMIN(env)).json()[0]

    # 先付一笔 1200 并确认到账
    r = env.client.post(
        f"/admin/treasury/grants/{grant['id']}/payments",
        headers=ADMIN(env),
        json={"request_no": "REQ-1", "amount_cents": 1200},
    )
    assert r.status_code == 201 and r.json()["status"] == "paid"

    # 再受理一笔 800，停在 accepted（模拟清算端失联）
    from app.clearing import service as clearing_service

    clearing_service.set_sim_control(env.clearing_db, offline=True)
    r = env.client.post(
        f"/admin/treasury/grants/{grant['id']}/payments",
        headers=ADMIN(env),
        json={"request_no": "REQ-2", "amount_cents": 800},
    )
    assert r.status_code == 201 and r.json()["status"] == "accepted"

    # 提出撤销提案并通过
    rbid = make_open_revoke_ballot(env, grant["id"])
    approve_ballot(env, rbid)

    g = env.client.get(f"/admin/treasury/grants/{grant['id']}", headers=ADMIN(env)).json()
    assert g["status"] == "revoked" and g["effective"] is False
    # 已付 1200 不追回；受理中 800 仍占着，未占用部分 = 3000-1200-800 = 1000 已退回
    pool = _pool(env)
    assert pool["reserved_cents"] == 800  # 只剩受理中那笔
    assert pool["paid_cents"] == 1200
    # 可用 = 入池 10000 - 仍保留 800 - 已付 1200
    assert pool["available_cents"] == 8000
    _assert_ledger_invariant(env)

    # 撤销后不再受理新付款
    r = env.client.post(
        f"/admin/treasury/grants/{grant['id']}/payments",
        headers=ADMIN(env),
        json={"request_no": "REQ-3", "amount_cents": 100},
    )
    assert r.status_code == 409

    # 受理中的款最终失败：清算端恢复，用同一请求号重试，首笔提交被明确判失败
    clearing_service.set_sim_control(env.clearing_db, offline=False, fail_next=True)
    r = env.client.post(
        f"/admin/treasury/grants/{grant['id']}/payments",
        headers=ADMIN(env),
        json={"request_no": "REQ-2", "amount_cents": 800},
    )
    assert r.status_code == 201 and r.json()["status"] == "failed"
    g = env.client.get(f"/admin/treasury/grants/{grant['id']}", headers=ADMIN(env)).json()
    assert g["reserved_cents"] == 0  # 拨款额度没有恢复
    pool = _pool(env)
    assert pool["reserved_cents"] == 0
    assert pool["available_cents"] == 10000 - 1200  # 只花掉已付的 1200
    _assert_ledger_invariant(env)


def test_revoked_grant_accepted_payment_still_completes_on_success(env):
    """撤销前已受理的款，撤销后清算端确认扣款成功：照常出池，不恢复拨款额度。"""
    from app.clearing import service as clearing_service

    _deposit(env, 5000)
    bid = make_open_ballot_with_proposal(env, "供应商A", 2000)
    approve_ballot(env, bid)
    grant = env.client.get("/admin/treasury/grants", headers=ADMIN(env)).json()[0]

    clearing_service.set_sim_control(env.clearing_db, offline=True)
    r = env.client.post(
        f"/admin/treasury/grants/{grant['id']}/payments",
        headers=ADMIN(env),
        json={"request_no": "REQ-PEND", "amount_cents": 900},
    )
    assert r.json()["status"] == "accepted"

    rbid = make_open_revoke_ballot(env, grant["id"])
    approve_ballot(env, rbid)
    # 撤销后该 900 仍被受理中付款占用
    assert _pool(env)["reserved_cents"] == 900

    # 清算端恢复，同请求号重试 → 清算端首次受理并扣款成功
    clearing_service.set_sim_control(env.clearing_db, offline=False)
    r = env.client.post(
        f"/admin/treasury/grants/{grant['id']}/payments",
        headers=ADMIN(env),
        json={"request_no": "REQ-PEND", "amount_cents": 900},
    )
    assert r.json()["status"] == "paid"
    g = env.client.get(f"/admin/treasury/grants/{grant['id']}", headers=ADMIN(env)).json()
    assert g["status"] == "revoked"
    assert g["reserved_cents"] == 0       # 拨款额度不恢复
    assert g["paid_cents"] == 900         # 钱确实付出去了
    pool = _pool(env)
    assert pool["reserved_cents"] == 0
    assert pool["paid_cents"] == 900
    assert pool["available_cents"] == 5000 - 900
    _assert_ledger_invariant(env)


def test_revoke_proposal_requires_active_grant(env):
    _deposit(env, 10000)
    bid = make_open_ballot_with_proposal(env, "供应商A", 1000)
    approve_ballot(env, bid)
    grant = env.client.get("/admin/treasury/grants", headers=ADMIN(env)).json()[0]

    # draft 表决上绑定不存在的拨款 → 404
    r = env.client.post("/admin/ballots", headers=ADMIN(env), json={"title": "撤销案0"})
    draft_id = r.json()["id"]
    r = env.client.post(
        f"/admin/ballots/{draft_id}/proposals/revoke",
        headers=ADMIN(env),
        json={"grant_id": 9999},
    )
    assert r.status_code == 404

    # 先撤销一次
    rbid = make_open_revoke_ballot(env, grant["id"])
    approve_ballot(env, rbid)

    # 对已撤销拨款再提撤销 → 409
    r = env.client.post("/admin/ballots", headers=ADMIN(env), json={"title": "撤销案2"})
    bid2 = r.json()["id"]
    r = env.client.post(
        f"/admin/ballots/{bid2}/proposals/revoke",
        headers=ADMIN(env),
        json={"grant_id": grant["id"]},
    )
    assert r.status_code == 409


def test_non_admin_cannot_use_treasury_endpoints(env):
    m = create_member(env, "M01", "张三")
    h = {"X-Member-Token": m["token"]}
    assert env.client.get("/admin/treasury/pool", headers=h).status_code == 403
    assert env.client.get("/admin/treasury/grants", headers=h).status_code == 403
    r = env.client.post(
        "/admin/treasury/pool/deposit", headers=h, json={"amount_cents": 100}
    )
    assert r.status_code == 403


def test_available_balance_accounts_for_already_paid_money(env):
    """已扣款出池的钱不能再被新拨款保留：可用额须扣除 paid_cents。"""
    _deposit(env, 1000)
    bid = make_open_ballot_with_proposal(env, "供应商A", 600)
    approve_ballot(env, bid)
    grant = env.client.get("/admin/treasury/grants", headers=ADMIN(env)).json()[0]
    # 用掉 600（拨款全额付完）
    r = env.client.post(
        f"/admin/treasury/grants/{grant['id']}/payments",
        headers=ADMIN(env),
        json={"request_no": "REQ-1", "amount_cents": 600},
    )
    assert r.json()["status"] == "paid"
    assert _pool(env)["available_cents"] == 400

    # 再提一个 500 的拨款提案：可用只有 400，必须判余额不足
    bid2 = make_open_ballot_with_proposal(env, "供应商B", 500)
    approve_ballot(env, bid2)
    p2 = env.client.get(f"/admin/ballots/{bid2}/proposal", headers=ADMIN(env)).json()
    assert p2["status"] == "insufficient_funds"
    assert "可用 400 分" in p2["note"]
    _assert_ledger_invariant(env)


# ---------------------------------------------------------------- 辅助
def make_open_ballot_with_proposal(
    env, payee: str, amount: int, voters: int = 3
) -> int:
    """建 draft 表决、绑定拨款提案、先建会员再开启，返回已开启的 ballot_id。"""
    r = env.client.post("/admin/ballots", headers=ADMIN(env), json={"title": "拨款案"})
    bid = r.json()["id"]
    r = env.client.post(
        f"/admin/ballots/{bid}/proposals/allocate",
        headers=ADMIN(env),
        json={"payee": payee, "max_amount_cents": amount},
    )
    assert r.status_code == 201, r.text
    for i in range(voters):
        create_member(env, f"V{bid}-{i:02d}", f"表决人{bid}-{i}")
    assert env.client.post(f"/admin/ballots/{bid}/open", headers=ADMIN(env)).status_code == 200
    return bid


def make_open_revoke_ballot(env, grant_id: int, voters: int = 3) -> int:
    """建 draft 撤销表决、绑定撤销提案、先建会员再开启。"""
    r = env.client.post("/admin/ballots", headers=ADMIN(env), json={"title": "撤销案"})
    bid = r.json()["id"]
    r = env.client.post(
        f"/admin/ballots/{bid}/proposals/revoke",
        headers=ADMIN(env),
        json={"grant_id": grant_id},
    )
    assert r.status_code == 201, r.text
    for i in range(voters):
        create_member(env, f"R{bid}-{i:02d}", f"撤销表决人{bid}-{i}")
    assert env.client.post(f"/admin/ballots/{bid}/open", headers=ADMIN(env)).status_code == 200
    return bid
