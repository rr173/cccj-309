"""资金侧并发测试：撤销 vs 付款受理、多提案争余额、同请求号并发重试。

所有写事务由 database.tx 的全局锁串行化，结果必须等价于某个确定的
服务端受理顺序，且资金池账目恒等式始终成立。
"""
from __future__ import annotations

import threading

from app import service, treasury
from app.clearing import service as clearing_service
from app.database import read

from .conftest import approve_ballot, create_member
from .test_treasury import (
    _deposit,
    _pool,
    make_open_ballot_with_proposal,
    make_open_revoke_ballot,
)

ADMIN = lambda env: {"X-Member-Token": env.admin["token"]}


def _run_collect(*fns):
    barrier = threading.Barrier(len(fns))
    results, errors = [], []

    def worker(fn):
        barrier.wait()
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def _grant(env, amount=1000, deposit=10000):
    _deposit(env, deposit)
    bid = make_open_ballot_with_proposal(env, "供应商A", amount)
    approve_ballot(env, bid)
    # 返回本轮新建的拨款（按 ballot 定位），而不是列表第一条
    proposal = treasury.get_proposal_by_ballot(env.db, bid)
    return treasury.get_grant(env.db, proposal["resulting_grant_id"])


def test_concurrent_payments_never_exceed_reservation(env):
    grant = _grant(env, amount=1000)
    # 两笔各 600，总和超过 1000：必有且仅有一笔被受理
    fns = [
        (lambda i=i: treasury.submit_payment(
            env.db, env.clearing_client, grant["id"], f"REQ-C{i}", 600
        ))
        for i in range(2)
    ]
    results, errors = _run_collect(*fns)
    assert len(results) == 1
    assert len(errors) == 1 and errors[0].status_code == 409
    g = treasury.get_grant(env.db, grant["id"])
    assert g["accepted_pending_cents"] + g["paid_cents"] == 600
    # 再来三笔把剩余 400 拆满
    fns = [
        (lambda i=i: treasury.submit_payment(
            env.db, env.clearing_client, grant["id"], f"REQ-S{i}", 200
        ))
        for i in range(3)
    ]
    results, errors = _run_collect(*fns)
    assert len(results) == 2 and len(errors) == 1
    g = treasury.get_grant(env.db, grant["id"])
    assert g["paid_cents"] == 1000 and g["reserved_cents"] == 0


def test_concurrent_same_request_no_never_double_holds(env):
    grant = _grant(env, amount=1000)
    fns = [
        lambda: treasury.submit_payment(
            env.db, env.clearing_client, grant["id"], "REQ-SAME", 300
        )
        for _ in range(4)
    ]
    results, errors = _run_collect(*fns)
    # 同一请求号并发重试：全部返回同一条付款，无 409、无重复占用
    assert errors == []
    assert len(results) == 4
    assert len({p["id"] for p in results}) == 1
    with read(env.db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM payments WHERE request_no='REQ-SAME'"
        ).fetchone()["n"]
        assert n == 1
    g = treasury.get_grant(env.db, grant["id"])
    assert g["paid_cents"] == 300  # 只扣一次


def test_concurrent_revoke_vs_payment_well_defined(env):
    # 让清算端整体宕机：付款受理后停在 accepted，才能观察“受理中占用”语义
    clearing_service.set_sim_control(env.clearing_db, offline=True)

    # 自包含单轮：只建一个拨款、一场撤销表决，反复重置并发起跑条件
    grant = _grant(env, amount=1000)
    rbid = make_open_revoke_ballot(env, grant["id"])
    pool_before = _pool(env)

    def do_pay():
        return treasury.submit_payment(
            env.db, env.clearing_client, grant["id"], "REQ-RACE", 400
        )

    def do_revoke():
        approve_ballot(env, rbid)

    results, errors = _run_collect(do_pay, do_revoke)
    g = treasury.get_grant(env.db, grant["id"])
    assert g["status"] == "revoked"
    pool_after = _pool(env)
    if errors:
        # 撤销先受理：付款被 409 拒绝，无受理中占用，本拨款 1000 全部退回
        assert len(errors) == 1 and errors[0].status_code == 409
        assert g["reserved_cents"] == 0
        assert pool_after["reserved_cents"] == pool_before["reserved_cents"] - 1000
    else:
        # 付款先受理（400 占用且停在 accepted）：撤销只退回剩余 600
        assert len(results) == 2
        assert g["reserved_cents"] == 400
        assert pool_after["reserved_cents"] == pool_before["reserved_cents"] - 600
    # 恒等式在任意受理顺序下都成立
    assert (
        pool_after["available_cents"]
        + pool_after["reserved_cents"]
        + pool_after["paid_cents"]
        == pool_after["deposited_cents"]
    )

    clearing_service.set_sim_control(env.clearing_db, offline=False)


def test_concurrent_proposals_compete_for_one_pool(env):
    _deposit(env, 1500)
    # 两个各需 1000 的拨款提案并行结算，资金只够一个
    bids = []
    for i in range(2):
        r = env.client.post(
            "/admin/ballots", headers=ADMIN(env), json={"title": f"案{i}"}
        )
        bid = r.json()["id"]
        treasury.create_allocate_proposal(env.db, bid, f"供应商{i}", 1000)
        # 先建会员、再开启，保证两场表决都有合法投票人
        create_member(env, f"CV{bid}-0", f"竞争表决人{bid}")
        env.client.post(f"/admin/ballots/{bid}/open", headers=ADMIN(env))
        c = env.client.post(
            f"/me/ballots/{bid}/credential",
            headers={"X-Member-Token": env.tokens[f"CV{bid}-0"]},
        ).json()["credential"]
        env.client.post(f"/ballots/{bid}/vote", json={"credential": c, "choice": "yes"})
        bids.append(bid)

    def settle(bid):
        return service.settle_ballot(env.db, bid)

    results, errors = _run_collect(
        (lambda: settle(bids[0])),
        (lambda: settle(bids[1])),
    )
    assert errors == []
    statuses = sorted(
        treasury.get_proposal_by_ballot(env.db, bid)["status"] for bid in bids
    )
    assert statuses == ["approved", "insufficient_funds"]
    grants = treasury.list_grants(env.db)
    assert len(grants) == 1
    assert _pool(env)["reserved_cents"] == 1000
    assert _pool(env)["available_cents"] == 500


# ---------------------------------------------------------------- 辅助
def treasury_bare_settle(env, bid):
    return service.settle_ballot(env.db, bid)
