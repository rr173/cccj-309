"""批次拆并、备货出库、质量隔离与召回联系的本地演示。

运行：
    PYTHONPATH=/tmp/pylibs python3 demo_inventory.py
    # 已安装 requirements 时也可直接：python3 demo_inventory.py

脚本使用进程内 TestClient 和独立 demo_inventory.db，不连接外部仓储、
通知或收款系统。重复运行会先删除旧演示库。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import service  # noqa: E402
from app.main import create_app  # noqa: E402

DB_PATH = ROOT / "demo_inventory.db"
SALT = "demo-inventory-salt"


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
    for path in (DB_PATH,):
        Path(path).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    app = create_app(db_path=str(DB_PATH), credential_salt=SALT,
                     start_reconciler=False)
    client = TestClient(app)
    admin = service.create_member(str(DB_PATH), SALT, "ADMIN", "合作社管理员", is_admin=True)
    headers = {"X-Member-Token": admin["token"]}

    banner("1. 入库：登记批次号、品种和正整数箱数")
    show("入库 A-2026-01（20 箱番茄）", client.post(
        "/admin/batches", headers=headers,
        json={"batch_no": "A-2026-01", "variety": "番茄", "cases": 20,
              "note": "东侧大棚"},
    ))
    show("入库 B-2026-02（10 箱番茄）", client.post(
        "/admin/batches", headers=headers,
        json={"batch_no": "B-2026-02", "variety": "番茄", "cases": 10},
    ))

    banner("2. 拆并：A 拆 6 箱、B 拆 4 箱，合成 C；余量留在原批次")
    c_batch = show("合成 C-2026-03（10 箱，保留全部来源）", client.post(
        "/admin/batches/merge", headers=headers,
        json={
            "new_batch_no": "C-2026-03",
            "variety": "番茄",
            "sources": [
                {"batch_no": "A-2026-01", "cases": 6},
                {"batch_no": "B-2026-02", "cases": 4},
            ],
        },
    ))

    banner("3. 第一单：指定 C 的 6 箱，备货后确认交货")
    order1 = show("创建绿野超市的 6 箱出库单", client.post(
        "/admin/outbound-orders", headers=headers,
        json={"order_no": "OUT-DEMO-1", "buyer": "绿野超市",
              "variety": "番茄", "total_cases": 6},
    ))
    show("从 C 批次备货 6 箱（占用库存）", client.post(
        f"/admin/outbound-orders/{order1['id']}/prepare",
        headers=headers,
        json={"allocations": [{"batch_no": "C-2026-03", "cases": 6}]},
    ))
    show("确认整单交货，固化实际批次与箱数", client.post(
        f"/admin/outbound-orders/{order1['id']}/deliver", headers=headers
    ))

    banner("4. 第二单：从 A 余量指定 5 箱并备货，等待交货")
    order2 = show("创建晨曦食堂的 5 箱出库单", client.post(
        "/admin/outbound-orders", headers=headers,
        json={"order_no": "OUT-DEMO-2", "buyer": "晨曦食堂",
              "variety": "番茄", "total_cases": 5},
    ))
    show("从 A 批次备货 5 箱", client.post(
        f"/admin/outbound-orders/{order2['id']}/prepare",
        headers=headers,
        json={"allocations": [{"batch_no": "A-2026-01", "cases": 5}]},
    ))

    banner("5. 登记 A 批次质量事件：C 是 A 的后代，自动进入召回；"
           "第二张已备货单整单退回并释放占用")
    event = show("登记质量事件", client.post(
        "/admin/quality-events", headers=headers,
        json={"batch_no": "A-2026-01", "reason": "抽检疑似农残异常，待复检",
              "severity": "high"},
    ))
    show("第二单当前为 pending（不会自动恢复备货）", client.get(
        f"/admin/outbound-orders/{order2['id']}", headers=headers
    ))
    show("A/C 均被隔离；C 仍可在事件详情中看到召回清单", client.get(
        f"/admin/quality-events/{event['id']}", headers=headers
    ))

    banner("6. 记录已联系绿野超市；同一订单若涉多个事件，联系状态各自独立")
    recall_id = event["recalls"][0]["id"]
    show("记下召回联系结果", client.post(
        f"/admin/recalls/{recall_id}/contact",
        headers=headers,
        json={"contacted": True, "contacted_by": "值班管理员",
              "note": "已电话通知，超市暂停销售并安排退回 6 箱。"},
    ))
    show("按事件查询召回清单", client.get(
        f"/admin/quality-events/{event['id']}/recalls", headers=headers
    ))

    banner("7. 解除事件：隔离结束，但召回清单和联系记录仍保留")
    show("解除 A 质量事件", client.post(
        f"/admin/quality-events/{event['id']}/resolve",
        headers=headers,
        json={"resolution_note": "复检合格；召回记录留存核对。"},
    ))

    banner("8. 重启模拟：用同一 SQLite 文件创建新应用，数据仍可办理/核对")
    restarted_app = create_app(
        db_path=str(DB_PATH), credential_salt=SALT, start_reconciler=False
    )
    restarted = TestClient(restarted_app)
    show("重启后查询批次", restarted.get("/admin/batches", headers=headers))
    show("重启后查询召回", restarted.get("/admin/recalls", headers=headers))

    banner("演示完成")
    print("可运行测试：PYTHONPATH=/tmp/pylibs python3 -m pytest tests/test_inventory.py -q")


if __name__ == "__main__":
    main()
