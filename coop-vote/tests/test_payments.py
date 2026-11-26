"""付款受理、幂等重试、失联不判失败、重启对账、清算端去重测试。"""
from __future__ import annotations

from app.clearing import service as clearing_service
from app.database import read

from .conftest import approve_ballot
from .test_treasury import _deposit, _pool, make_open_ballot_with_proposal

ADMIN = lambda env: {"X-Member-Token": env.admin["token"]}


def _active_grant(env, amount: int = 3000, deposit: int = 10000):
    _deposit(env, deposit)
    bid = make_open_ballot_with_proposal(env, "供应商A", amount)
    approve_ballot(env, bid)
    return env.client.get("/admin/treasury/grants", headers=ADMIN(env)).json()[0]


def _pay(env, grant_id: int, request_no: str, amount: int):
    return env.client.post(
        f"/admin/treasury/grants/{grant_id}/payments",
        headers=ADMIN(env),
        json={"request_no": request_no, "amount_cents": amount},
    )


def test_payment_happy_path_moves_accepted_to_paid(env):
    grant = _active_grant(env)
    r = _pay(env, grant["id"], "REQ-1", 1200)
    assert r.status_code == 201
    p = r.json()
    assert p["status"] == "paid"
    assert p["payee"] == "供应商A"
    assert p["amount_cents"] == 1200

    g = env.client.get(f"/admin/treasury/grants/{grant['id']}", headers=ADMIN(env)).json()
    assert g["paid_cents"] == 1200
    assert g["reserved_cents"] == 1800
    assert g["accepted_pending_cents"] == 0
    pool = _pool(env)
    assert pool["reserved_cents"] == 1800  # 已扣的 1200 已离开资金池保留


def test_payment_must_be_positive_and_within_grant(env):
    grant = _active_grant(env, amount=1000)
    r = _pay(env, grant["id"], "REQ-X", 0)
    assert r.status_code == 422
    # 超出拨款额度
    r = _pay(env, grant["id"], "REQ-1", 600)
    assert r.status_code == 201
    r = _pay(env, grant["id"], "REQ-2", 500)  # 剩 400，超额
    assert r.status_code == 409
    # 额度内的分次付款可以继续
    r = _pay(env, grant["id"], "REQ-3", 400)
    assert r.status_code == 201 and r.json()["status"] == "paid"


def test_accepted_payment_occupies_quota_and_retry_is_idempotent(env):
    grant = _active_grant(env, amount=1000)
    clearing_service.set_sim_control(env.clearing_db, offline=True)

    r = _pay(env, grant["id"], "REQ-1", 600)
    assert r.status_code == 201 and r.json()["status"] == "accepted"
    g = env.client.get(f"/admin/treasury/grants/{grant['id']}", headers=ADMIN(env)).json()
    assert g["accepted_pending_cents"] == 600
    assert g["available_for_payment_cents"] == 400

    # 受理中即占用：再发 500 会超额
    r = _pay(env, grant["id"], "REQ-2", 500)
    assert r.status_code == 409

    # 同一请求号重试：返回原付款，不重复占用（请求号不同才会新增占用）
    r = _pay(env, grant["id"], "REQ-1", 600)
    assert r.status_code == 201
    p = r.json()
    assert p["status"] == "accepted"
    g = env.client.get(f"/admin/treasury/grants/{grant['id']}", headers=ADMIN(env)).json()
    assert g["accepted_pending_cents"] == 600  # 没有翻倍

    # 同一请求号改金额重用 → 409
    r = _pay(env, grant["id"], "REQ-1", 601)
    assert r.status_code == 409 and "不得改金额重用" in r.json()["detail"]


def test_retry_same_request_after_debit_returns_original_paid_status(env):
    grant = _active_grant(env)
    # 清算端扣款后断连
    clearing_service.set_sim_control(env.clearing_db, drop_after_debit=True)
    r = _pay(env, grant["id"], "REQ-DROP", 700)
    assert r.status_code == 201 and r.json()["status"] == "accepted"  # 失联不判失败

    # 清算端其实已扣款，关掉断连开关后用同一请求号重试
    clearing_service.set_sim_control(env.clearing_db, drop_after_debit=False)
    r = _pay(env, grant["id"], "REQ-DROP", 700)
    assert r.status_code == 201
    p = r.json()
    assert p["status"] == "paid"

    # 清算端只有一笔流水（去重，没有扣两次）
    with read(env.clearing_db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM clearing_payments WHERE ref=?",
            (p["clearing_ref"],),
        ).fetchone()["n"]
        assert n == 1
    g = env.client.get(f"/admin/treasury/grants/{grant['id']}", headers=ADMIN(env)).json()
    assert g["paid_cents"] == 700  # 只付了一次


def test_outage_keeps_accepted_and_manual_reconcile_completes_after_recovery(env):
    grant = _active_grant(env)
    # 先用“整体宕机”让付款停在受理中（清算端完全不可达，查询也失败）
    clearing_service.set_sim_control(env.clearing_db, offline=True)
    r = _pay(env, grant["id"], "REQ-1", 300)
    assert r.json()["status"] == "accepted"
    payment_id = r.json()["id"]

    # 宕机期间单笔对账拿不到可读结果，状态不变
    r = env.client.post(
        f"/admin/treasury/payments/{payment_id}/reconcile", headers=ADMIN(env)
    )
    assert r.json()["status"] == "accepted"

    # 清算端恢复：此时它还没见过这笔付款，查询 404 同样视为失联未知，
    # 保持 accepted，由同请求号重试完成首次扣款（见下一个测试）。
    clearing_service.set_sim_control(env.clearing_db, offline=False)
    r = env.client.post(
        f"/admin/treasury/payments/{payment_id}/reconcile", headers=ADMIN(env)
    )
    assert r.json()["status"] == "accepted"

    # 同请求号重试 → 清算端首次受理并扣款，主服务转为 paid
    r = _pay(env, grant["id"], "REQ-1", 300)
    assert r.status_code == 201 and r.json()["status"] == "paid"

    p = env.client.get(f"/admin/treasury/payments/{payment_id}", headers=ADMIN(env)).json()
    assert p["status"] == "paid"
    with read(env.clearing_db) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM clearing_payments").fetchone()["n"] == 1


def test_full_outage_then_retry_after_recovery_does_not_double_pay(env):
    """清算端全程宕机：付款只在主服务落为 accepted；恢复后同请求号重试，
    清算端首次收到并去重，绝不扣两次。"""
    grant = _active_grant(env)
    clearing_service.set_sim_control(env.clearing_db, offline=True)
    r = _pay(env, grant["id"], "REQ-OUT", 350)
    assert r.json()["status"] == "accepted"
    payment_id = r.json()["id"]

    clearing_service.set_sim_control(env.clearing_db, offline=False)
    # 同一请求号重试 → 受理记录复用，清算端第一次真正收到这笔付款
    r = _pay(env, grant["id"], "REQ-OUT", 350)
    assert r.json()["status"] == "paid" and r.json()["id"] == payment_id
    with read(env.clearing_db) as conn:
        rows = conn.execute("SELECT * FROM clearing_payments").fetchall()
        assert len(rows) == 1 and rows[0]["amount_cents"] == 350


def test_explicit_clearing_failure_releases_hold_back_to_grant(env):
    grant = _active_grant(env, amount=1000)
    clearing_service.set_sim_control(env.clearing_db, fail_next=True)
    r = _pay(env, grant["id"], "REQ-FAIL", 400)
    assert r.status_code == 201 and r.json()["status"] == "failed"

    g = env.client.get(f"/admin/treasury/grants/{grant['id']}", headers=ADMIN(env)).json()
    # 拨款生效中：占用退回拨款额度，可再发起新付款
    assert g["reserved_cents"] == 1000
    assert g["paid_cents"] == 0
    assert g["available_for_payment_cents"] == 1000
    r = _pay(env, grant["id"], "REQ-RETRY", 1000)
    assert r.status_code == 201 and r.json()["status"] == "paid"
    assert _pool(env)["reserved_cents"] == 0


def test_payment_status_query_by_id_and_request_no(env):
    grant = _active_grant(env)
    created = _pay(env, grant["id"], "REQ-Q", 250).json()
    r = env.client.get(f"/admin/treasury/payments/{created['id']}", headers=ADMIN(env))
    assert r.status_code == 200 and r.json()["request_no"] == "REQ-Q"
    r = env.client.get(
        f"/admin/treasury/grants/{grant['id']}/payments", headers=ADMIN(env)
    )
    assert [p["request_no"] for p in r.json()] == ["REQ-Q"]
    # 不存在的请求号
    r = env.client.get("/admin/treasury/payments", headers=ADMIN(env))
    assert r.status_code == 200 and len(r.json()) == 1


def test_clearing_dedup_guarantees_no_double_debit_on_resubmit(env):
    grant = _active_grant(env)
    first = _pay(env, grant["id"], "REQ-DEDUP", 500).json()
    # 直接对清算端用同一 ref 再提交一次（模拟服务端任意重试）
    result = env.clearing_client.submit_payment(
        first["clearing_ref"], "供应商A", 500
    )
    assert result["status"] == "paid"
    with read(env.clearing_db) as conn:
        rows = conn.execute("SELECT * FROM clearing_payments").fetchall()
        assert len(rows) == 1
        assert rows[0]["amount_cents"] == 500


def test_accepted_payment_survives_server_restart_and_reconciles(env):
    """重启不丢付款、不付两次：用新 app 实例模拟进程重启。"""
    from fastapi.testclient import TestClient

    from app.clearing_client import ClearingClient
    from app.main import create_app
    from tests.conftest import SyncASGITransport

    grant = _active_grant(env)
    clearing_service.set_sim_control(env.clearing_db, drop_after_debit=True)
    r = _pay(env, grant["id"], "REQ-RESTART", 650)
    assert r.json()["status"] == "accepted"
    ref = r.json()["clearing_ref"]

    # 模拟服务重启：新建主服务应用（同一主库文件），清算端仍在线且已扣款
    clearing_service.set_sim_control(env.clearing_db, drop_after_debit=False)
    new_transport = SyncASGITransport(env.clearing_app, drop_payments=True)
    new_client = ClearingClient(base_url="http://clearing-test", transport=new_transport)
    restarted = create_app(
        db_path=env.db,
        credential_salt=env.salt,
        clearing_client=new_client,
        start_reconciler=False,
    )
    with TestClient(restarted) as rc:  # lifespan 启动即对账
        # 启动对账已把 accepted 补成 paid
        r = rc.get(
            f"/admin/treasury/grants/{grant['id']}/payments",
            headers={"X-Member-Token": env.admin["token"]},
        )
        payments = r.json()
        assert len(payments) == 1
        assert payments[0]["status"] == "paid"
        assert payments[0]["clearing_ref"] == ref

    # 清算端仍只有一笔扣款
    with read(env.clearing_db) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM clearing_payments").fetchone()["n"] == 1
