"""批次拆并、占用出库、质量隔离、召回联系与并发/重启测试。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import inventory
from app.main import create_app
from app.service import ServiceError
from tests.conftest import create_member


def h(env):
    return {"X-Member-Token": env.admin["token"]}


def post(env, path, payload=None, headers=None):
    return env.client.post(path, headers=headers or h(env), json=payload or {})


def get(env, path, headers=None):
    return env.client.get(path, headers=headers or h(env))


def make_batch(env, no="B1", variety="番茄", cases=10):
    r = post(env, "/admin/batches", {"batch_no": no, "variety": variety, "cases": cases})
    assert r.status_code == 201, r.text
    return r.json()


def compose(env, new_no, sources, variety="番茄"):
    r = post(env, "/admin/batches/merge", {
        "new_batch_no": new_no,
        "variety": variety,
        "sources": [
            {"batch_no": no, "cases": qty} if isinstance(no, str) else no
            for no, qty in sources
        ],
    })
    assert r.status_code == 201, r.text
    return r.json()


def make_order(env, buyer="买家甲", total=5, variety="番茄", order_no=None):
    payload = {"buyer": buyer, "variety": variety, "total_cases": total}
    if order_no:
        payload["order_no"] = order_no
    r = post(env, "/admin/outbound-orders", payload)
    assert r.status_code == 201, r.text
    return r.json()


def allocate(env, oid, allocations):
    return post(env, f"/admin/outbound-orders/{oid}/prepare", {"allocations": allocations})


def deliver(env, oid):
    return post(env, f"/admin/outbound-orders/{oid}/deliver")


def quality_event(env, batch_no, reason="抽检异常"):
    r = post(env, "/admin/quality-events", {"batch_no": batch_no, "reason": reason})
    assert r.status_code == 201, r.text
    return r.json()


def test_batch_registration_validation_and_admin_only(env):
    create_member(env, "M_INV", "库存会员")
    member_h = {"X-Member-Token": env.tokens["M_INV"]}

    r = env.client.post("/admin/batches", headers=member_h,
                        json={"batch_no": "X", "variety": "番茄", "cases": 1})
    assert r.status_code == 403

    r = env.client.post("/admin/batches", headers={},
                        json={"batch_no": "X", "variety": "番茄", "cases": 1})
    assert r.status_code == 401

    for bad_cases in (0, -1, 1.5, "abc", True):
        r = post(env, "/admin/batches",
                 {"batch_no": f"BAD-{bad_cases}", "variety": "番茄", "cases": bad_cases})
        assert r.status_code in (400, 422), (bad_cases, r.status_code, r.text)

    r = post(env, "/admin/batches", {"batch_no": "DUP", "variety": "番茄", "cases": 2})
    assert r.status_code == 201
    r = post(env, "/admin/batches", {"batch_no": "DUP", "variety": "番茄", "cases": 3})
    assert r.status_code == 409


def test_split_merge_keeps_remainder_total_and_lineage(env):
    make_batch(env, "A", cases=10)
    make_batch(env, "B", cases=8)

    c = compose(env, "C", [("A", 4), ("B", 3)])
    assert c["cases"] == 7
    assert [(s["batch_no"], s["cases"]) for s in c["sources"]] == [("A", 4), ("B", 3)]

    a = get(env, "/admin/batches/A").json()
    b = get(env, "/admin/batches/B").json()
    assert a["cases"] == 6
    assert b["cases"] == 5
    assert a["available_cases"] == 6
    assert b["available_cases"] == 5

    # C 再与 A 的一部分合成 D；闭包保留 A/B 两类来源。
    d = compose(env, "D", [("C", 2), ("A", 1)])
    assert d["cases"] == 3
    source_nos = set(d["source_batch_nos"])
    assert {"A", "B", "C"} <= source_nos

    # 总在库箱数在拆并前后守恒：6 + 5 + (7-2) + (3) = 19 = 10+8+1? Wait initial 18.
    # After C: A6+B5+C7=18. After D takes C2 and A1: A5, C5, D3, B5 => 18.
    e = post(env, "/admin/batches/A/split", {
        "batch_id": 1, "cases": 1, "new_batch_no": "E"
    })
    assert e.status_code == 201, e.text
    assert e.json()["cases"] == 1
    batches = get(env, "/admin/batches").json()
    assert sum(x["cases"] for x in batches) == 18

    # 品种不一致不能合并。
    make_batch(env, "R", variety="黄瓜", cases=1)
    r = post(env, "/admin/batches/merge", {
        "new_batch_no": "BAD",
        "sources": [{"batch_no": "A", "cases": 1}, {"batch_no": "R", "cases": 1}],
    })
    assert r.status_code == 409


def test_prepare_occupies_then_cancel_releases(env):
    make_batch(env, "A", cases=10)
    o1 = make_order(env, total=5)
    r = allocate(env, o1["id"], [{"batch_no": "A", "cases": 5}])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "prepared"

    a = get(env, "/admin/batches/A").json()
    assert a["reserved_cases"] == 5
    assert a["available_cases"] == 5

    # 另一单最多只能占用剩余 5 箱；第 6 箱失败且整单不进入 prepared。
    o2 = make_order(env, buyer="买家乙", total=6)
    r = allocate(env, o2["id"], [{"batch_no": "A", "cases": 6}])
    assert r.status_code == 409
    o2 = get(env, f"/admin/outbound-orders/{o2['id']}").json()
    assert o2["status"] == "pending"

    o3 = make_order(env, buyer="买家丙", total=5)
    r = allocate(env, o3["id"], [{"batch_no": "A", "cases": 5}])
    assert r.status_code == 200

    # 取消第一单释放占用，但第三单仍占用 5。
    r = post(env, f"/admin/outbound-orders/{o1['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    a = get(env, "/admin/batches/A").json()
    assert a["reserved_cases"] == 5
    assert a["available_cases"] == 5

    # 取消幂等。
    assert post(env, f"/admin/outbound-orders/{o1['id']}/cancel").status_code == 200


def test_partial_allocation_must_sum_to_order_total(env):
    make_batch(env, "A", cases=10)
    o = make_order(env, total=5)

    r = env.client.put(
        f"/admin/outbound-orders/{o['id']}/allocations",
        headers=h(env),
        json={"allocations": [{"batch_no": "A", "cases": 2}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["allocated_cases"] == 2
    assert body["remaining_cases"] == 3

    r = allocate(env, o["id"], [{"batch_no": "A", "cases": 2}])
    assert r.status_code == 409
    assert "凑齐" in r.json()["detail"]

    r = allocate(env, o["id"], [{"batch_no": "A", "cases": 5}])
    assert r.status_code == 200
    assert r.json()["status"] == "prepared"


def test_delivery_reduces_stock_and_keeps_actual_details(env):
    make_batch(env, "A", cases=10)
    o = make_order(env, buyer="北国超市", total=7, order_no="OUT-TEST-1")
    assert allocate(env, o["id"], [{"batch_no": "A", "cases": 7}]).status_code == 200

    r = deliver(env, o["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "delivered"
    assert body["delivery"]["items"][0]["batch_no"] == "A"
    assert body["delivery"]["items"][0]["cases"] == 7

    a = get(env, "/admin/batches/A").json()
    assert a["case_count"] == 3
    assert a["delivered_cases"] == 7
    assert a["available_cases"] == 3

    # 已交货的 7 箱不能再拆；剩余 3 箱可以拆。
    r = post(env, "/admin/batches/merge", {
        "new_batch_no": "TOO_MUCH",
        "sources": [{"batch_no": "A", "cases": 4}],
    })
    assert r.status_code == 409
    assert compose(env, "LEFT", [("A", 3)])["cases"] == 3

    # 交货确认重试幂等，不重复扣库存。
    assert deliver(env, o["id"]).status_code == 200
    a = get(env, "/admin/batches/A").json()
    assert a["delivered_cases"] == 7


def test_quality_event_returns_prepared_order_and_releases_all_allocations(env):
    make_batch(env, "A", cases=10)
    make_batch(env, "B", cases=10)

    affected_order = make_order(env, buyer="受影响买家", total=8)
    r = allocate(env, affected_order["id"], [
        {"batch_no": "A", "cases": 3}, {"batch_no": "B", "cases": 5}
    ])
    assert r.status_code == 200

    unaffected_order = make_order(env, buyer="其他买家", total=7)
    assert allocate(env, unaffected_order["id"], [{"batch_no": "A", "cases": 7}]).status_code == 200

    event = quality_event(env, "B")
    assert event["released_order_ids"] == [affected_order["id"]]

    affected = get(env, f"/admin/outbound-orders/{affected_order['id']}").json()
    unaffected = get(env, f"/admin/outbound-orders/{unaffected_order['id']}").json()
    assert affected["status"] == "pending"
    assert unaffected["status"] == "prepared"

    a = get(env, "/admin/batches/A").json()
    b = get(env, "/admin/batches/B").json()
    # 受影响整单的 A3+B5 全部释放；只有另一单 A7 仍占用。
    assert a["reserved_cases"] == 7
    assert a["available_cases"] == 3
    assert b["reserved_cases"] == 0
    assert b["available_cases"] == 10
    assert b["quarantined"] is True

    # 隔离期不能用 B 备货，也不能拆 B。
    r = allocate(env, affected_order["id"], [
        {"batch_no": "A", "cases": 3}, {"batch_no": "B", "cases": 5}
    ])
    assert r.status_code == 409
    r = post(env, "/admin/batches/merge", {
        "new_batch_no": "BLOCKED", "sources": [{"batch_no": "B", "cases": 1}]
    })
    assert r.status_code == 409

    # 解除事件不自动恢复备货；需管理员重新备货。
    eid = event["id"]
    r = post(env, f"/admin/quality-events/{eid}/resolve", {"note": "复检合格"})
    assert r.status_code == 200
    affected = get(env, f"/admin/outbound-orders/{affected_order['id']}").json()
    assert affected["status"] == "pending"
    assert get(env, "/admin/batches/B").json()["quarantined"] is False
    assert allocate(env, affected_order["id"], [
        {"batch_no": "A", "cases": 3}, {"batch_no": "B", "cases": 5}
    ]).status_code == 200


def test_recall_lists_buyer_order_actual_batches_and_dedups_diamond(env):
    make_batch(env, "A", cases=10)
    compose(env, "B", [("A", 5)])
    compose(env, "C", [("A", 5)])
    compose(env, "D", [("B", 3), ("C", 3)])

    o1 = make_order(env, buyer="买家甲", total=3)
    assert allocate(env, o1["id"], [{"batch_no": "D", "cases": 3}]).status_code == 200
    assert deliver(env, o1["id"]).status_code == 200

    o2 = make_order(env, buyer="买家乙", total=2)
    assert allocate(env, o2["id"], [{"batch_no": "B", "cases": 2}]).status_code == 200
    assert deliver(env, o2["id"]).status_code == 200

    event_a = quality_event(env, "A", "农残复核")
    recalls = event_a["recalls"]
    assert {r["order_id"] for r in recalls} == {o1["id"], o2["id"]}

    r1 = next(r for r in recalls if r["order_id"] == o1["id"])
    assert r1["buyer_name"] == "买家甲"
    assert r1["order_no"] == o1["order_no"]
    # D 经 B-A 与 C-A 两条路径受影响，但同一交付明细只出现一次。
    assert len(r1["items"]) == 1
    assert r1["items"][0]["batch_no"] == "D"
    assert r1["items"][0]["cases"] == 3

    r2 = next(r for r in recalls if r["order_id"] == o2["id"])
    assert r2["items"][0]["batch_no"] == "B"
    assert r2["items"][0]["cases"] == 2

    # 同一订单还涉及针对 D 的独立事件；两个联系状态互不影响。
    event_d = quality_event(env, "D", "包装异物")
    d_recall = next(r for r in event_d["recalls"] if r["order_id"] == o1["id"])
    assert d_recall["contacted"] is False

    r = post(env, f"/admin/quality-events/{event_a['id']}/orders/{o1['id']}/contact",
             {"note": "已电话通知并安排召回"})
    assert r.status_code == 200
    assert r.json()["contacted"] is True
    assert r.json()["contact_note"] == "已电话通知并安排召回"

    event_a_after = get(env, f"/admin/quality-events/{event_a['id']}").json()
    case_a = next(r for r in event_a_after["recalls"] if r["order_id"] == o1["id"])
    case_d = get(env, f"/admin/recalls/{d_recall['id']}").json()
    assert case_a["contacted"] is True
    assert case_d["contacted"] is False

    # 解除 A 后召回清单与联系状态仍在；D 事件未解除前 D 仍隔离。
    assert post(env, f"/admin/quality-events/{event_a['id']}/resolve").status_code == 200
    event_a_after = get(env, f"/admin/quality-events/{event_a['id']}").json()
    assert len(event_a_after["recalls"]) == 2
    case_a = next(r for r in event_a_after["recalls"] if r["order_id"] == o1["id"])
    assert case_a["contacted"] is True
    assert get(env, "/admin/batches/D").json()["quarantined"] is True

    assert post(env, f"/admin/quality-events/{event_d['id']}/orders/{o1['id']}/contact",
                {"contacted": True}).status_code == 200
    assert post(env, f"/admin/quality-events/{event_d['id']}/resolve").status_code == 200
    assert get(env, "/admin/batches/D").json()["quarantined"] is False


def test_concurrent_prepare_and_deliver_never_overcommits(env):
    make_batch(env, "CONC", cases=100)
    order_ids = []
    for i in range(20):
        o = make_order(env, buyer=f"买家{i}", total=10)
        order_ids.append(o["id"])

    def prepare_one(oid):
        return env.client.post(
            f"/admin/outbound-orders/{oid}/prepare",
            headers=h(env),
            json={"allocations": [{"batch_no": "CONC", "cases": 10}]},
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(prepare_one, order_ids))

    assert codes.count(200) == 10
    assert codes.count(409) == 10

    batch = get(env, "/admin/batches/CONC").json()
    assert batch["reserved_cases"] == 100
    assert batch["available_cases"] == 0

    prepared_ids = [
        oid for oid, code in zip(order_ids, codes) if code == 200
    ]

    def deliver_one(oid):
        return deliver(env, oid).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        deliver_codes = list(pool.map(deliver_one, prepared_ids))
    assert deliver_codes == [200] * 10

    batch = get(env, "/admin/batches/CONC").json()
    assert batch["case_count"] == 0
    assert batch["delivered_cases"] == 100
    assert batch["reserved_cases"] == 0

    orders = get(env, "/admin/outbound-orders").json()
    assert sum(1 for o in orders if o["status"] == "delivered") == 10
    assert sum(1 for o in orders if o["status"] == "pending") == 10


def test_quality_event_race_with_prepare_ends_in_one_accepted_order(env):
    make_batch(env, "RACEP", cases=200)
    order_ids = [make_order(env, buyer=f"备单{i}", total=10)["id"] for i in range(20)]
    barrier = threading.Barrier(len(order_ids) + 1)

    def prepare_one(oid):
        barrier.wait()
        try:
            inventory.prepare_order(
                env.db, oid, [{"batch_no": "RACEP", "cases": 10}]
            )
            return "prepared"
        except ServiceError as exc:
            return exc.status_code

    def raise_event():
        barrier.wait()
        return inventory.create_quality_event(env.db, "RACEP")["id"]

    with ThreadPoolExecutor(max_workers=len(order_ids) + 1) as pool:
        prepare_futures = [pool.submit(prepare_one, oid) for oid in order_ids]
        event_future = pool.submit(raise_event)
        codes = [f.result() for f in prepare_futures]
        event_id = event_future.result()

    event = inventory.get_quality_event(env.db, event_id)
    orders = inventory.list_orders(env.db)
    assert all(o["status"] == "pending" for o in orders)
    assert codes.count("prepared") == len(event["released_order_ids"])

    batch = get(env, "/admin/batches/RACEP").json()
    assert batch["reserved_cases"] == 0
    assert batch["available_cases"] == 200
    assert batch["quarantined"] is True


def test_quality_event_race_with_delivery_never_half_delivers(env):
    make_batch(env, "RACED", cases=100)
    order_ids = []
    for i in range(10):
        oid = make_order(env, buyer=f"交单{i}", total=5)["id"]
        assert allocate(env, oid, [{"batch_no": "RACED", "cases": 5}]).status_code == 200
        order_ids.append(oid)

    barrier = threading.Barrier(len(order_ids) + 1)

    def deliver_one(oid):
        barrier.wait()
        try:
            return inventory.deliver_order(env.db, oid)["status"]
        except ServiceError as exc:
            return exc.status_code

    def raise_event():
        barrier.wait()
        return inventory.create_quality_event(env.db, "RACED")["id"]

    with ThreadPoolExecutor(max_workers=11) as pool:
        deliver_futures = [pool.submit(deliver_one, oid) for oid in order_ids]
        event_future = pool.submit(raise_event)
        codes = [f.result() for f in deliver_futures]
        event_id = event_future.result()

    event = inventory.get_quality_event(env.db, event_id)
    orders = inventory.list_orders(env.db)
    delivered = [o for o in orders if o["status"] == "delivered"]
    pending = [o for o in orders if o["status"] == "pending"]
    assert len(delivered) + len(pending) == 10
    assert not [o for o in orders if o["status"] == "prepared"]
    assert codes.count("delivered") == len(delivered)

    batch = get(env, "/admin/batches/RACED").json()
    assert batch["delivered_cases"] == 5 * len(delivered)
    assert batch["case_count"] == 100 - 5 * len(delivered)
    assert batch["reserved_cases"] == 0

    assert len(event["recalls"]) == len(delivered)
    recall_cases = sum(item["cases"] for r in event["recalls"] for item in r["items"])
    assert recall_cases == 5 * len(delivered)


def test_restart_preserves_batches_orders_events_and_contacts(env):
    make_batch(env, "PERSIST", cases=9)
    o = make_order(env, buyer="重启买家", total=4)
    assert allocate(env, o["id"], [{"batch_no": "PERSIST", "cases": 4}]).status_code == 200
    assert deliver(env, o["id"]).status_code == 200
    event = quality_event(env, "PERSIST")
    post(env, f"/admin/recalls/{event['recalls'][0]['id']}/contact", {"note": "已联系"})

    # 用同一个 SQLite 文件创建全新应用/客户端，模拟服务重启。
    restarted_app = create_app(
        db_path=env.db,
        credential_salt=env.salt,
        clearing_client=env.clearing_client,
        start_reconciler=False,
    )
    client = TestClient(restarted_app)
    new_h = {"X-Member-Token": env.admin["token"]}

    r = client.get("/admin/batches/PERSIST", headers=new_h)
    assert r.status_code == 200
    assert r.json()["delivered_cases"] == 4

    r = client.get(f"/admin/outbound-orders/{o['id']}", headers=new_h)
    assert r.status_code == 200
    assert r.json()["status"] == "delivered"

    r = client.get(f"/admin/quality-events/{event['id']}", headers=new_h)
    assert r.status_code == 200
    assert r.json()["recalls"][0]["contacted"] is True
    assert r.json()["recalls"][0]["contact_note"] == "已联系"
