"""机具成套预约 → 可行性递补 → 实物交接 → 逐件归还的本地演示。

运行：
    PYTHONPATH=/tmp/pylibs python3 demo_equipment.py
    # 若已自行安装 requirements，也可直接：python3 demo_equipment.py

演示使用进程内 TestClient，不监听端口、不接外部系统。为了在几秒内展示
“到达结束时间/逾期”等懒状态，脚本把服务端时钟固定在一个演示时刻并显式推进；
真实运行时使用系统 UTC 时间，查看或办理接口会自动完成同样的状态推进。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import equipment, service  # noqa: E402
from app.main import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "demo_equipment.db"
T0 = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)
clock = {"now": T0}


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def show(title: str, response) -> dict:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        payload = response.text
    print(f"\n— {title} [HTTP {response.status_code}]")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if response.status_code >= 400:
        raise SystemExit(f"演示请求失败：{title}: {response.status_code} {payload}")
    return payload


def main() -> None:
    # 可重复演示：删除旧 SQLite 主库及 WAL/SHM。
    for path in (DB_PATH,):
        Path(path).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    # 演示时钟就是 equipment 服务认定的“服务端当前时间”。
    equipment.utc_now = lambda: clock["now"].isoformat()

    app = create_app(db_path=str(DB_PATH), credential_salt="demo-equipment-salt",
                     start_reconciler=False)
    client = TestClient(app)

    admin = service.create_member(
        str(DB_PATH), "demo-equipment-salt", "ADMIN", "合作社管理员", is_admin=True
    )
    admin_h = {"X-Member-Token": admin["token"]}

    banner("1. 管理员登记有独立编号和能力标签的机具")
    for asset_no, name, tags in [
        ("TRC-01", "大马力拖拉机", ["牵引", "50马力"]),
        ("TRC-02", "普通拖拉机", ["牵引"]),
        ("DRL-01", "多功能钻孔机", ["钻孔", "充电"]),
    ]:
        show(
            f"登记 {asset_no}",
            client.post(
                "/admin/equipment",
                headers=admin_h,
                json={"asset_no": asset_no, "name": name, "tags": tags},
            ),
        )

    banner("2. 复用本地会员身份建立两名会员")
    tokens = {}
    for code, name in [("M01", "张三"), ("M02", "李四")]:
        body = show(
            f"创建会员 {code}",
            client.post(
                "/admin/members",
                headers=admin_h,
                json={"code": code, "name": name, "is_admin": False},
            ),
        )
        tokens[code] = body["token"]
    h1 = {"X-Member-Token": tokens["M01"]}
    h2 = {"X-Member-Token": tokens["M02"]}

    start = T0 + timedelta(hours=2)
    end = T0 + timedelta(hours=5)
    checklist = [
        {"label": "主机牵引", "tags": ["牵引", "50马力"]},
        {"label": "钻孔组件", "tags": ["钻孔", "充电"]},
    ]

    banner("3. 张三提交成套预约：两项都满足才确认")
    r1 = show(
        "张三的整套申请（初始确认，给出可派实物但不锁序列号）",
        client.post(
            "/me/equipment/reservations",
            headers=h1,
            json={"starts_at": start.isoformat(), "ends_at": end.isoformat(),
                  "items": checklist},
        ),
    )

    banner("4. 李四申请同一重叠时段的同一套：数量/能力不足，整单等候")
    r2 = show(
        "李四的整套申请（waiting）",
        client.post(
            "/me/equipment/reservations",
            headers=h2,
            json={"starts_at": start.isoformat(), "ends_at": end.isoformat(),
                  "items": checklist},
        ),
    )

    banner("5. 管理员登记 TRC-01 维修停用：旧确认先尝试保留，不能保留则降为等候")
    show(
        "登记 TRC-01 在借用时段维修",
        client.post(
            "/admin/maintenance",
            headers=admin_h,
            json={"asset_no": "TRC-01",
                  "starts_at": start.isoformat(), "ends_at": end.isoformat(),
                  "reason": "液压保养"},
        ),
    )
    show("张三预约状态（应变为 waiting）",
         client.get(f"/me/equipment/reservations/{r1['id']}", headers=h1))

    banner("6. 撤销维修：先保留更早受理的张三，李四仍等候")
    windows = show("维修时段列表", client.get("/admin/maintenance", headers=admin_h))
    wid = windows[0]["id"]
    show("撤销维修", client.post(f"/admin/maintenance/{wid}/revoke", headers=admin_h))
    show("张三恢复 confirmed",
         client.get(f"/me/equipment/reservations/{r1['id']}", headers=h1))
    show("李四仍 waiting",
         client.get(f"/me/equipment/reservations/{r2['id']}", headers=h2))

    banner("7. 张三取消尚未领取的预约；李四按受理顺序递补确认")
    show("张三取消", client.post(
        f"/me/equipment/reservations/{r1['id']}/cancel", headers=h1))
    r2 = show("李四已递补为 confirmed",
              client.get(f"/me/equipment/reservations/{r2['id']}", headers=h2))

    banner("8. 时钟到达借用开始时间，管理员一次领齐，系统给出实物编号")
    clock["now"] = start
    handover = show(
        "办理交接（未指定序列号，由系统选择并校验不影响其他确认）",
        client.post(f"/admin/reservations/{r2['id']}/handover", headers=admin_h),
    )
    loan_ids = [loan["id"] for loan in handover["loans"]]

    banner("9. 管理员核对实际领出机具；会员端也显示已领取")
    show("全部未归还借约", client.get("/admin/loans?open=true", headers=admin_h))
    show("李四查看本人预约",
         client.get(f"/me/equipment/reservations/{r2['id']}", headers=h2))

    banner("10. 逐件归还钻孔机；拖拉机仍在外借")
    drill_loan_id = next(
        item["loan"]["id"]
        for item in handover["items"]
        if "钻孔" in item["tags"]
    )
    show("归还钻孔机", client.post(
        f"/admin/loans/{drill_loan_id}/return", headers=admin_h))
    show("管理员再次核对外借中实物",
         client.get("/admin/loans?open=true", headers=admin_h))

    banner("11. 到达约定结束时间但拖拉机未实际归还：查看时自动标逾期并停止承诺")
    clock["now"] = end
    show("逾期借约（无需定时任务，查询即反映）",
         client.get("/admin/loans?overdue=true", headers=admin_h))
    future_start = end
    future_end = end + timedelta(hours=1)
    r3 = show(
        "张三申请紧接着的 50 马力牵引机具；TRC-01 逾期未还，只能等候",
        client.post(
            "/me/equipment/reservations",
            headers=h1,
            json={
                "starts_at": future_start.isoformat(),
                "ends_at": future_end.isoformat(),
                "items": [{"tags": ["牵引", "50马力"]}],
            },
        ),
    )

    banner("12. 实际归还 TRC-01 后，等候单立即递补并可再次交接")
    show("按实物编号办理归还", client.post(
        "/admin/equipment/returns",
        headers=admin_h,
        json={"asset_no": "TRC-01"},
    ))
    r3 = show("张三的新预约已 confirmed",
              client.get(f"/me/equipment/reservations/{r3['id']}", headers=h1))
    show("再次交接并立即归还，机具完成复用",
         client.post(f"/admin/reservations/{r3['id']}/handover", headers=admin_h))
    show("逐件归还新借约", client.post(
        "/admin/equipment/returns", headers=admin_h, json={"asset_no": "TRC-01"}
    ))

    banner("演示完成")
    print(
        f"SQLite 数据库：{DB_PATH}\n"
        "可使用以下方式启动真实 HTTP 服务：\n"
        "  COOPVOTE_DB=./demo_equipment.db PYTHONPATH=/tmp/pylibs \\\n"
        "  python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000\n"
        "接口文档：http://127.0.0.1:8000/docs\n"
        "重启服务后，未归还实物与预约承诺仍保存在 SQLite 中。"
    )


if __name__ == "__main__":
    main()
