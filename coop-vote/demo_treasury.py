"""资金拨款/付款端到端演示（无需手动开两个终端）。

故事线：
  1. 启动独立进程内的清算模拟端（独立 SQLite、独立 uvicorn 服务）；
  2. 资金池入金 → 拨款提案 → 匿名表决通过 → 全额保留、拨款生效；
  3. 分次付款：正常扣款、同请求号幂等重试、超额拒绝；
  4. 清算端“扣款后断连”：主服务不判失败，恢复后对账补齐且不重复扣款；
  5. 清算端整体宕机：付款保持受理中，重启主服务也不丢，恢复后补齐；
  6. 撤销提案通过：未占用额度立即退回、已付不追回、受理中款失败后退回资金池；
  7. 余额不足的拨款提案：票数照常公布，但拨款不生效。

运行：.venv/bin/python demo_treasury.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.clearing.app import create_clearing_app  # noqa: E402
from app.clearing_client import ClearingClient  # noqa: E402
from app.main import create_app  # noqa: E402

CLEAR_PORT = 8091


def banner(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def show(step: str, resp) -> None:
    print(f"\n[{step}] HTTP {resp.status_code}")
    print(resp.json())


def start_clearing(clearing_db: str):
    config = uvicorn.Config(
        create_clearing_app(clearing_db),
        host="127.0.0.1",
        port=CLEAR_PORT,
        log_level="error",  # 演示里“扣款后断连”会打印预期内的连接中断栈，降噪
    )
    server = uvicorn.Server(config)
    thread = __import__("threading").Thread(target=server.run, daemon=True)
    thread.start()
    # 等待端口就绪
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{CLEAR_PORT}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    return server


def sim_control(base: str, **flags) -> None:
    r = httpx.post(f"{base}/sim/control", json=flags, timeout=5)
    print("  清算端故障注入 ->", r.json())


def main() -> None:
    root = Path(__file__).resolve().parent
    db_path = str(root / "demo_treasury.db")
    clearing_db = str(root / "demo_clearing.db")
    for p in (db_path, clearing_db):
        Path(p).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(p + suffix).unlink(missing_ok=True)

    banner("0. 启动独立的本地清算模拟端（独立进程/独立数据库）")
    start_clearing(clearing_db)
    clearing_base = f"http://127.0.0.1:{CLEAR_PORT}"
    print(f"清算端已上线：{clearing_base}，数据库 {clearing_db}")

    clearing = ClearingClient(clearing_base)
    app = create_app(
        db_path=db_path,
        credential_salt="demo-salt",
        clearing_client=clearing,
        start_reconciler=True,
    )
    client = TestClient(app)

    from app import service

    admin = service.create_member(db_path, "demo-salt", "ADMIN01", "名册管理员", is_admin=True)
    admin_h = {"X-Member-Token": admin["token"]}
    # 预备 4 名表决会员（先建档，后开启表决，才能进入冻结范围）
    for code, name in [("M01", "张三"), ("M02", "李四"), ("M03", "王五"), ("M04", "赵六")]:
        client.post("/admin/members", headers=admin_h, json={"code": code, "name": name})

    banner("1. 资金池入金 10000 分（100 元）")
    show("入金", client.post("/admin/treasury/pool/deposit",
                             headers=admin_h, json={"amount_cents": 10000}))

    banner("2. 提出拨款提案：固定收款方『有机农场』，上限 3000 分")
    r = client.post("/admin/ballots", headers=admin_h, json={"title": "采购拨款案"})
    bid = r.json()["id"]
    show("绑定拨款提案（draft 阶段）", client.post(
        f"/admin/ballots/{bid}/proposals/allocate",
        headers=admin_h,
        json={"payee": "有机农场", "max_amount_cents": 3000},
    ))
    show("开启表决（此后提案冻结）", client.post(f"/admin/ballots/{bid}/open", headers=admin_h))

    banner("3. 会员匿名投票：3 赞成、1 反对")
    # 演示用服务层完成领凭证+匿名投票（业务路径与 HTTP 接口完全一致）
    _vote_via_service(db_path, "demo-salt", bid, {"M01": "yes", "M02": "yes",
                                                  "M03": "yes", "M04": "no"})
    show("结算（赞成 > 反对，决议通过并立即拨款）",
         client.post(f"/admin/ballots/{bid}/settle", headers=admin_h))
    show("资金池（3000 分已全额保留）", client.get("/admin/treasury/pool", headers=admin_h))
    grants = client.get("/admin/treasury/grants", headers=admin_h).json()
    grant_id = grants[0]["id"]
    show("拨款状态（生效）", client.get(f"/admin/treasury/grants/{grant_id}", headers=admin_h))

    banner("4. 分次付款：1200 分正常扣款")
    show("付款 REQ-1", client.post(
        f"/admin/treasury/grants/{grant_id}/payments",
        headers=admin_h, json={"request_no": "REQ-1", "amount_cents": 1200},
    ))
    show("同请求号重试（幂等，返回原付款，不重复扣款）", client.post(
        f"/admin/treasury/grants/{grant_id}/payments",
        headers=admin_h, json={"request_no": "REQ-1", "amount_cents": 1200},
    ))
    show("同请求号改金额（被拒）", client.post(
        f"/admin/treasury/grants/{grant_id}/payments",
        headers=admin_h, json={"request_no": "REQ-1", "amount_cents": 999},
    ))
    show("再付 2000 分（超过剩余额度，被拒）", client.post(
        f"/admin/treasury/grants/{grant_id}/payments",
        headers=admin_h, json={"request_no": "REQ-2", "amount_cents": 2000},
    ))

    banner("5. 清算端扣款后断连：主服务不判失败，恢复后对账补齐")
    sim_control(clearing_base, drop_after_debit=True)
    show("付款 REQ-3（清算端已扣款但响应断开，主服务保持受理中）", client.post(
        f"/admin/treasury/grants/{grant_id}/payments",
        headers=admin_h, json={"request_no": "REQ-3", "amount_cents": 700},
    ))
    sim_control(clearing_base, drop_after_debit=False)
    show("手动对账（补齐为已付，清算端只扣一次）",
         client.post("/admin/treasury/reconcile", headers=admin_h))

    banner("6. 清算端整体宕机：付款保持受理中，主服务重启也不丢失")
    sim_control(clearing_base, offline=True)
    show("宕机期间付款 REQ-4（落库受理、占用额度，等待清算端）", client.post(
        f"/admin/treasury/grants/{grant_id}/payments",
        headers=admin_h, json={"request_no": "REQ-4", "amount_cents": 300},
    ))
    sim_control(clearing_base, offline=False)
    show("恢复后同请求号重试（首次送达清算端并扣款）", client.post(
        f"/admin/treasury/grants/{grant_id}/payments",
        headers=admin_h, json={"request_no": "REQ-4", "amount_cents": 300},
    ))

    banner("7. 撤销拨款：未占用额度立即退回，已付款不追回")
    r = client.post("/admin/ballots", headers=admin_h, json={"title": "撤销拨款案"})
    rbid = r.json()["id"]
    client.post(f"/admin/ballots/{rbid}/proposals/revoke",
                headers=admin_h, json={"grant_id": grant_id})
    client.post(f"/admin/ballots/{rbid}/open", headers=admin_h)
    _vote_via_service(db_path, "demo-salt", rbid, {"M01": "yes", "M02": "yes",
                                                   "M03": "yes", "M04": "no"})
    show("结算撤销案", client.post(f"/admin/ballots/{rbid}/settle", headers=admin_h))
    show("撤销后资金池", client.get("/admin/treasury/pool", headers=admin_h))
    show("撤销后拨款（不再受理新付款）",
         client.get(f"/admin/treasury/grants/{grant_id}", headers=admin_h))
    show("尝试新付款（被拒）", client.post(
        f"/admin/treasury/grants/{grant_id}/payments",
        headers=admin_h, json={"request_no": "REQ-X", "amount_cents": 100},
    ))

    banner("8. 余额不足的拨款提案：票数照常公布，拨款不生效")
    r = client.post("/admin/ballots", headers=admin_h, json={"title": "超额拨款案"})
    bad_bid = r.json()["id"]
    client.post(f"/admin/ballots/{bad_bid}/proposals/allocate",
                headers=admin_h, json={"payee": "供应商X", "max_amount_cents": 999999})
    client.post(f"/admin/ballots/{bad_bid}/open", headers=admin_h)
    _vote_via_service(db_path, "demo-salt", bad_bid, {"M01": "yes", "M02": "yes",
                                                      "M03": "yes", "M04": "no"})
    show("结算（赞成 > 反对，但余额不足）",
         client.post(f"/admin/ballots/{bad_bid}/settle", headers=admin_h))
    show("该提案状态（insufficient_funds，不生效、以后也不自动生效）",
         client.get(f"/admin/ballots/{bad_bid}/proposal", headers=admin_h))
    show("公开端仍只给三项总数", client.get(f"/public/ballots/{bad_bid}"))

    banner("最终资金池与全部付款")
    show("资金池", client.get("/admin/treasury/pool", headers=admin_h))
    show("全部拨款", client.get("/admin/treasury/grants", headers=admin_h))
    show("全部付款", client.get("/admin/treasury/payments", headers=admin_h))
    print("\n演示完成。")


def _vote_via_service(db_path: str, salt: str, ballot_id: int, choices: dict[str, str]) -> None:
    """演示辅助：用服务层完成领凭证+投票，避免在演示里转存会员明文令牌。"""
    from app import service
    from app.database import read

    with read(db_path) as conn:
        rows = conn.execute(
            "SELECT m.id AS member_id, m.code AS code FROM eligibilities e "
            "JOIN members m ON m.id = e.member_id WHERE e.ballot_id=? ORDER BY m.id",
            (ballot_id,),
        ).fetchall()
    for row in rows:
        if row["code"] not in choices:
            continue
        cred = service.issue_credential(db_path, salt, ballot_id, row["member_id"])
        service.cast_vote(db_path, salt, ballot_id, cred["credential"], choices[row["code"]])


if __name__ == "__main__":
    main()
