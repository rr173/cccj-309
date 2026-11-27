"""成套机具预约、重算递补、交接与归还的端到端测试。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import equipment
from app.main import create_app
from tests.conftest import create_member

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": T0}
    monkeypatch.setattr(equipment, "utc_now", lambda: state["now"].isoformat())
    return state


def admin_headers(env):
    return {"X-Member-Token": env.admin["token"]}


def member_headers(env, code):
    return {"X-Member-Token": env.tokens[code]}


def create_equipment(env, asset_no, tags, name=None):
    r = env.client.post(
        "/admin/equipment",
        headers=admin_headers(env),
        json={"asset_no": asset_no, "name": name or asset_no, "tags": tags},
    )
    assert r.status_code == 201, r.text
    return r.json()


def add_maintenance(env, equipment_pk, start, end, reason="保养"):
    r = env.client.post(
        f"/admin/equipment/{equipment_pk}/maintenance",
        headers=admin_headers(env),
        json={"starts_at": start.isoformat(), "ends_at": end.isoformat(), "reason": reason},
    )
    assert r.status_code == 201, r.text
    return r.json()


def reserve(env, code, start, end, items):
    return env.client.post(
        "/me/equipment/reservations",
        headers=member_headers(env, code),
        json={
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "items": items,
        },
    )


def get_my_reservation(env, code, rid):
    return env.client.get(
        f"/me/equipment/reservations/{rid}", headers=member_headers(env, code)
    )


def test_equipment_registration_requires_admin_and_unique_number(env):
    create_member(env, "M01", "会员甲")

    r = env.client.post(
        "/admin/equipment",
        headers=member_headers(env, "M01"),
        json={"asset_no": "T-1", "tags": ["牵引"]},
    )
    assert r.status_code == 403

    eq = create_equipment(env, "T-1", ["牵引", "50马力"])
    assert eq["state"] == "available"
    assert eq["tags"] == ["50马力", "牵引"] or eq["tags"] == ["牵引", "50马力"]

    r = env.client.post(
        "/admin/equipment",
        headers=admin_headers(env),
        json={"asset_no": "T-1", "tags": []},
    )
    assert r.status_code == 409


def test_full_set_confirms_or_waits_and_same_machine_cannot_fill_two_items(env, clock):
    create_member(env, "M01", "会员甲")
    create_equipment(env, "A1", ["A"])

    start, end = T0 + timedelta(hours=1), T0 + timedelta(hours=3)
    r = reserve(
        env,
        "M01",
        start,
        end,
        [{"label": "第一件", "tags": ["A"]}, {"label": "第二件", "tags": ["A"]}],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "waiting"
    assert all(item["planned_asset_no"] is None for item in body["items"])

    # 增加第二台 A 能力机具后，查看机具/预约会触发重算，整套确认且两件不同号。
    create_equipment(env, "A2", ["A"])
    body = env.client.get(
        "/me/equipment/reservations", headers=member_headers(env, "M01")
    ).json()[0]
    assert body["status"] == "confirmed"
    asset_numbers = [item["planned_asset_no"] for item in body["items"]]
    assert len(asset_numbers) == 2
    assert len(set(asset_numbers)) == 2


def test_item_requires_all_tags_and_time_is_left_closed_right_open(env, clock):
    create_member(env, "M01", "甲")
    create_member(env, "M02", "乙")
    # 只有一台同时有 A+B；另一台只有 A。
    create_equipment(env, "AB", ["A", "B"])
    create_equipment(env, "A", ["A"])

    # 要求 A+B 只能由 AB 覆盖。
    r1 = reserve(env, "M01", T0, T0 + timedelta(hours=2), [{"tags": ["A", "B"]}])
    assert r1.json()["status"] == "confirmed"

    # 同一台 AB 在相接的下一时段可再次承诺：[T0+2h, T0+3h)。
    r2 = reserve(env, "M02", T0 + timedelta(hours=2), T0 + timedelta(hours=3),
                 [{"tags": ["A", "B"]}])
    assert r2.status_code == 201, r2.text
    assert r2.json()["status"] == "confirmed"

    # 重叠时不能共用 AB；只有 A 标签的机具不满足 A+B。
    r3 = reserve(env, "M02", T0 + timedelta(hours=1), T0 + timedelta(hours=3),
                 [{"tags": ["A", "B"]}])
    assert r3.json()["status"] == "waiting"


def test_start_must_not_be_earlier_than_server_time(env, clock):
    create_member(env, "M01", "甲")
    create_equipment(env, "A1", ["A"])

    r = reserve(env, "M01", T0 - timedelta(seconds=1), T0 + timedelta(hours=1),
                [{"tags": ["A"]}])
    assert r.status_code == 422

    r = reserve(env, "M01", T0 + timedelta(hours=2), T0 + timedelta(hours=1),
                [{"tags": ["A"]}])
    assert r.status_code == 422

    # 等于服务端当前时间允许。
    r = reserve(env, "M01", T0, T0 + timedelta(hours=1), [{"tags": ["A"]}])
    assert r.status_code == 201
    assert r.json()["status"] == "confirmed"


def test_cancel_promotes_waitlist_in_acceptance_order_and_owner_only(env, clock):
    for code in ("M01", "M02", "M03"):
        create_member(env, code, code)
    create_equipment(env, "A1", ["A"])
    start, end = T0 + timedelta(hours=1), T0 + timedelta(hours=3)

    r1 = reserve(env, "M01", start, end, [{"tags": ["A"]}]).json()
    r2 = reserve(env, "M02", start, end, [{"tags": ["A"]}]).json()
    r3 = reserve(env, "M03", start, end, [{"tags": ["A"]}]).json()
    assert [r1["status"], r2["status"], r3["status"]] == [
        "confirmed", "waiting", "waiting"
    ]

    # M02 不能取消 M01 的单。
    r = env.client.post(
        f"/me/equipment/reservations/{r1['id']}/cancel",
        headers=member_headers(env, "M02"),
    )
    assert r.status_code == 404

    r = env.client.post(
        f"/me/equipment/reservations/{r1['id']}/cancel",
        headers=member_headers(env, "M01"),
    )
    assert r.status_code == 200
    r2_after = get_my_reservation(env, "M02", r2["id"]).json()
    r3_after = get_my_reservation(env, "M03", r3["id"]).json()
    assert r2_after["status"] == "confirmed"
    assert r3_after["status"] == "waiting"

    env.client.post(
        f"/me/equipment/reservations/{r2['id']}/cancel",
        headers=member_headers(env, "M02"),
    )
    r3_after = get_my_reservation(env, "M03", r3["id"]).json()
    assert r3_after["status"] == "confirmed"


def test_expired_uncollected_order_frees_and_backfills_remaining_period(env, clock):
    create_member(env, "M01", "甲")
    create_member(env, "M02", "乙")
    create_equipment(env, "A1", ["A"])

    r1 = reserve(env, "M01", T0 + timedelta(hours=1), T0 + timedelta(hours=3),
                 [{"tags": ["A"]}]).json()
    # 第二单 [2h,4h) 与第一单重叠，初始等候。
    r2 = reserve(env, "M02", T0 + timedelta(hours=2), T0 + timedelta(hours=4),
                 [{"tags": ["A"]}]).json()
    assert r1["status"] == "confirmed"
    assert r2["status"] == "waiting"

    clock["now"] = T0 + timedelta(hours=3)
    body1 = get_my_reservation(env, "M01", r1["id"]).json()
    body2 = get_my_reservation(env, "M02", r2["id"]).json()
    assert body1["status"] == "expired"
    assert body1["expired_at"] == (T0 + timedelta(hours=3)).isoformat()
    # 第一单失效后，第二单只需覆盖 [T+3h, T+4h) 剩余借期并被递补。
    assert body2["status"] == "confirmed"
    assert body2["in_handover_window"] is True


def test_availability_change_preserves_earlier_confirmed_and_skips_impossible_order(env, clock):
    for code in ("M01", "M02", "M03"):
        create_member(env, code, code)
    a1 = create_equipment(env, "A1", ["A"])
    create_equipment(env, "A2", ["A"])
    a3 = create_equipment(env, "A3", ["A"])
    create_equipment(env, "B1", ["B"])

    start, end = T0 + timedelta(hours=1), T0 + timedelta(hours=4)
    r1 = reserve(env, "M01", start, end, [{"tags": ["A"]}]).json()
    r2 = reserve(
        env, "M02", start, end, [{"tags": ["A"]}, {"tags": ["A"]}]
    ).json()
    r3 = reserve(env, "M03", start, end, [{"tags": ["B"]}]).json()
    assert all(x["status"] == "confirmed" for x in (r1, r2, r3))

    # 停用一台 A：A 能力只剩两台，无法同时满足 R1 的一件和 R2 的两件。
    add_maintenance(env, a3["id"], start, end)
    rows = env.client.get(
        "/admin/reservations", headers=admin_headers(env)
    ).json()
    by_id = {x["id"]: x for x in rows}
    assert by_id[r1["id"]]["status"] == "confirmed"
    assert by_id[r2["id"]]["status"] == "waiting"
    # R2 被略过后，不依赖 A 的 R3 仍作为前面已保留订单继续保留。
    assert by_id[r3["id"]]["status"] == "confirmed"

    # 撤销维修后，等候组按原受理顺序把 R2 递补回来。
    windows = env.client.get("/admin/maintenance", headers=admin_headers(env)).json()
    wid = windows[0]["id"]
    revoked = env.client.post(
        f"/admin/maintenance/{wid}/revoke", headers=admin_headers(env)
    )
    assert revoked.status_code == 200
    rows = env.client.get(
        "/admin/reservations", headers=admin_headers(env)
    ).json()
    by_id = {x["id"]: x for x in rows}
    assert by_id[r1["id"]]["status"] == "confirmed"
    assert by_id[r2["id"]]["status"] == "confirmed"
    assert by_id[r3["id"]]["status"] == "confirmed"


def test_waiting_group_skips_impossible_order_and_continues(env, clock):
    create_member(env, "M01", "甲")
    create_member(env, "M02", "乙")
    create_equipment(env, "A1", ["A"])
    create_equipment(env, "B1", ["B"])

    impossible = reserve(
        env, "M01", T0 + timedelta(hours=1), T0 + timedelta(hours=3),
        [{"tags": ["A"]}, {"tags": ["A"]}],
    ).json()
    okay = reserve(
        env, "M02", T0 + timedelta(hours=1), T0 + timedelta(hours=3),
        [{"tags": ["B"]}],
    ).json()
    assert impossible["status"] == "waiting"
    assert okay["status"] == "confirmed"

    rows = env.client.get("/admin/reservations", headers=admin_headers(env)).json()
    by_id = {row["id"]: row for row in rows}
    assert by_id[impossible["id"]]["status"] == "waiting"
    assert by_id[okay["id"]]["status"] == "confirmed"


def test_handover_gives_all_asset_numbers_and_supports_piecewise_return(env, clock):
    create_member(env, "M01", "甲")
    create_equipment(env, "AB", ["A", "B"])
    create_equipment(env, "B2", ["B"])

    r = reserve(
        env,
        "M01",
        T0,
        T0 + timedelta(hours=2),
        [{"tags": ["A", "B"]}, {"tags": ["B"]}],
    ).json()
    assert r["status"] == "confirmed"

    resp = env.client.post(
        f"/admin/reservations/{r['id']}/handover", headers=admin_headers(env)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "collected"
    assert body["collected_at"] == T0.isoformat()
    asset_numbers = [item["loan"]["asset_no"] for item in body["items"]]
    assert len(asset_numbers) == 2
    assert len(set(asset_numbers)) == 2

    # 重试交接幂等，不产生第二组借约。
    again = env.client.post(
        f"/admin/reservations/{r['id']}/handover", headers=admin_headers(env)
    ).json()
    assert [loan["id"] for loan in again["loans"]] == [loan["id"] for loan in body["loans"]]

    loans = env.client.get("/admin/loans?open=true", headers=admin_headers(env)).json()
    assert len(loans) == 2

    first_loan_id = body["loans"][0]["id"]
    returned = env.client.post(
        f"/admin/loans/{first_loan_id}/return", headers=admin_headers(env)
    ).json()
    assert returned["status"] == "returned"
    detail = get_my_reservation(env, "M01", r["id"]).json()
    assert detail["status"] == "collected"
    assert detail["active_loan_count"] == 1

    second_loan_id = next(l["id"] for l in body["loans"] if l["id"] != first_loan_id)
    env.client.post(
        f"/admin/loans/{second_loan_id}/return", headers=admin_headers(env)
    )
    detail = get_my_reservation(env, "M01", r["id"]).json()
    assert detail["status"] == "returned"
    assert detail["all_returned"] is True


def test_manual_selection_cannot_break_other_confirmed_reservation(env, clock):
    create_member(env, "M01", "甲")
    create_member(env, "M02", "乙")
    ab = create_equipment(env, "AB", ["A", "B"])
    a1 = create_equipment(env, "A1", ["A"])

    r1 = reserve(env, "M01", T0, T0 + timedelta(hours=3),
                 [{"tags": ["A", "B"]}]).json()
    r2 = reserve(env, "M02", T0, T0 + timedelta(hours=3),
                 [{"tags": ["A"]}]).json()
    assert r1["status"] == "confirmed"
    assert r2["status"] == "confirmed"

    # 错误地把唯一 AB 选给只要求 A 的 R2，会使 R1 无法兑现。
    bad = env.client.post(
        f"/admin/reservations/{r2['id']}/handover",
        headers=admin_headers(env),
        json={"asset_nos": ["AB"]},
    )
    assert bad.status_code == 409
    detail = get_my_reservation(env, "M02", r2["id"]).json()
    assert detail["status"] == "confirmed"
    assert all(item["loan"] is None for item in detail["items"])

    # 选择 A1 后，R1 仍可兑现。
    ok = env.client.post(
        f"/admin/reservations/{r2['id']}/handover",
        headers=admin_headers(env),
        json={"asset_nos": ["A1"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["items"][0]["loan"]["asset_no"] == "A1"

    r1_after = env.client.get(
        f"/admin/reservations/{r1['id']}", headers=admin_headers(env)
    ).json()
    assert r1_after["status"] == "confirmed"
    assert r1_after["items"][0]["planned_asset_no"] == "AB"

    # 同一台不能充当一套中的两件。
    r3 = reserve(env, "M01", T0 + timedelta(hours=4), T0 + timedelta(hours=6),
                 [{"tags": ["A"]}, {"tags": ["A"]}]).json()
    dup = env.client.post(
        f"/admin/reservations/{r3['id']}/handover",
        headers=admin_headers(env),
        json={"asset_nos": ["AB", "AB"]},
    )
    assert dup.status_code in (409, 422)


def test_due_but_unreturned_equipment_is_not_promised_until_return(env, clock):
    create_member(env, "M01", "甲")
    create_member(env, "M02", "乙")
    create_equipment(env, "A1", ["A"])

    r1 = reserve(env, "M01", T0, T0 + timedelta(hours=2), [{"tags": ["A"]}]).json()
    env.client.post(
        f"/admin/reservations/{r1['id']}/handover", headers=admin_headers(env)
    )

    # 未到期时，预计 T+2h 可还，相接时段可以先承诺。
    clock["now"] = T0 + timedelta(hours=1)
    r2 = reserve(
        env,
        "M02",
        T0 + timedelta(hours=2),
        T0 + timedelta(hours=3),
        [{"tags": ["A"]}],
    )
    assert r2.json()["status"] == "confirmed"

    # 到约定结束仍未实际归还：R2 不能继续确认，机具/借约显示逾期。
    clock["now"] = T0 + timedelta(hours=2)
    r2_detail = get_my_reservation(env, "M02", r2.json()["id"]).json()
    assert r2_detail["status"] == "waiting"

    loans = env.client.get("/admin/loans?overdue=true", headers=admin_headers(env)).json()
    assert len(loans) == 1
    assert loans[0]["is_overdue"] is True
    eq = env.client.get("/admin/equipment", headers=admin_headers(env)).json()[0]
    assert eq["state"] == "overdue"

    # 实际归还后，等候单立即递补。
    returned = env.client.post(
        "/admin/equipment/returns",
        headers=admin_headers(env),
        json={"asset_no": "A1"},
    )
    assert returned.status_code == 200, returned.text
    r2_detail = get_my_reservation(env, "M02", r2.json()["id"]).json()
    assert r2_detail["status"] == "confirmed"


def test_maintenance_does_not_return_checked_out_equipment(env, clock):
    create_member(env, "M01", "甲")
    eq = create_equipment(env, "A1", ["A"])
    r1 = reserve(env, "M01", T0, T0 + timedelta(hours=3), [{"tags": ["A"]}]).json()
    env.client.post(
        f"/admin/reservations/{r1['id']}/handover", headers=admin_headers(env)
    )

    add_maintenance(env, eq["id"], T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    detail = get_my_reservation(env, "M01", r1["id"]).json()
    assert detail["status"] == "collected"
    assert detail["active_loan_count"] == 1
    assert detail["items"][0]["loan"]["returned_at"] is None

    loans = env.client.get("/admin/loans?open=true", headers=admin_headers(env)).json()
    assert len(loans) == 1


def test_restart_preserves_loans_and_commitments(env, clock):
    create_member(env, "M01", "甲")
    create_equipment(env, "A1", ["A"])
    r1 = reserve(env, "M01", T0, T0 + timedelta(hours=2), [{"tags": ["A"]}]).json()
    env.client.post(
        f"/admin/reservations/{r1['id']}/handover", headers=admin_headers(env)
    )

    # 用同一个 SQLite 文件构造一个“重启后”的新应用。
    restarted_app = create_app(
        db_path=env.db,
        credential_salt=env.salt,
        clearing_client=env.clearing_client,
        start_reconciler=False,
    )
    with TestClient(restarted_app) as client2:
        loans = client2.get("/admin/loans", headers=admin_headers(env)).json()
        assert len(loans) == 1
        assert loans[0]["asset_no"] == "A1"
        detail = client2.get(
            f"/admin/reservations/{r1['id']}", headers=admin_headers(env)
        ).json()
        assert detail["status"] == "collected"


def test_concurrent_reservations_never_overconfirm(env, clock):
    for i in range(8):
        create_member(env, f"C{i:02d}", f"会员{i}")
    create_equipment(env, "A1", ["A"])
    start, end = T0 + timedelta(hours=1), T0 + timedelta(hours=3)

    def one(i):
        code = f"C{i:02d}"
        r = reserve(env, code, start, end, [{"tags": ["A"]}])
        assert r.status_code == 201, r.text
        return r.json()["status"], r.json()["id"], code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, range(8)))

    assert sum(status == "confirmed" for status, _, _ in results) == 1
    assert sum(status == "waiting" for status, _, _ in results) == 7

    confirmed = next((rid for status, rid, _ in results if status == "confirmed"), None)
    owner = next(code for status, _, code in results if status == "confirmed")
    r = env.client.post(
        f"/me/equipment/reservations/{confirmed}/cancel",
        headers=member_headers(env, owner),
    )
    assert r.status_code == 200
    rows = env.client.get("/admin/reservations", headers=admin_headers(env)).json()
    active = [x for x in rows if x["status"] in ("confirmed", "waiting")]
    assert sum(x["status"] == "confirmed" for x in active) == 1


def test_concurrent_handover_same_reservation_creates_one_loan_set(env, clock):
    create_member(env, "M01", "甲")
    create_equipment(env, "A1", ["A"])
    create_equipment(env, "A2", ["A"])
    rid = reserve(
        env,
        "M01",
        T0,
        T0 + timedelta(hours=2),
        [{"tags": ["A"]}, {"tags": ["A"]}],
    ).json()["id"]

    def handover(_):
        r = env.client.post(
            f"/admin/reservations/{rid}/handover", headers=admin_headers(env)
        )
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(handover, range(8)))

    assert all(code == 200 for code, _ in results)
    loan_id_sets = [{loan["id"] for loan in body["loans"]} for _, body in results]
    assert all(ids == loan_id_sets[0] for ids in loan_id_sets)
    assert len(loan_id_sets[0]) == 2
    loans = env.client.get("/admin/loans", headers=admin_headers(env)).json()
    assert len(loans) == 2
