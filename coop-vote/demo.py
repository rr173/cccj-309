"""本地端到端演示：无需启动服务器即可跑通完整表决故事线。

运行：.venv/bin/python demo.py
使用独立的演示数据库文件，可重复执行（每次重建）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import create_app  # noqa: E402

YES, NO, ABSTAIN = "yes", "no", "abstain"


def banner(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def show(step: str, resp) -> None:
    print(f"\n[{step}] HTTP {resp.status_code}")
    body = resp.json()
    # 演示输出里把过长的令牌截断，便于阅读
    if isinstance(body, dict) and "credential" in body:
        body = {**body, "credential": body["credential"][:18] + "…(截断)"}
    if isinstance(body, dict) and "token" in body:
        body = {**body, "token": body["token"][:18] + "…(截断)"}
    print(body)


def main() -> None:
    db_path = str(Path(__file__).resolve().parent / "demo.db")
    Path(db_path).unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(db_path + suffix).unlink(missing_ok=True)

    client = TestClient(create_app(db_path=db_path, credential_salt="demo-salt"))

    banner("1. 名册管理员建档（首位管理员由服务层引导播种）")
    from app import service

    admin = service.create_member(db_path, "demo-salt", "ADMIN01", "名册管理员", is_admin=True)
    admin_h = {"X-Member-Token": admin["token"]}
    print("管理员令牌（仅本次返回）:", admin["token"][:18], "…(截断)")

    members = {}
    for code, name in [("M01", "张三"), ("M02", "李四"), ("M03", "王五"), ("M04", "赵六")]:
        r = client.post("/admin/members", headers=admin_h, json={"code": code, "name": name})
        members[code] = r.json()
        show(f"建档 {name}", r)

    banner("2. 创建并开启表决 → 开启瞬间冻结会员范围")
    r = client.post("/admin/ballots", headers=admin_h, json={"title": "2026 年度预算案"})
    ballot_id = r.json()["id"]
    show("创建表决", r)
    show("开启表决（冻结范围）", client.post(f"/admin/ballots/{ballot_id}/open", headers=admin_h))

    # 开启后才加入的会员不在范围内
    r = client.post("/admin/members", headers=admin_h, json={"code": "M05", "name": "钱七"})
    late = r.json()
    show("开启后新入会（不在冻结范围）", r)

    banner("3. 会员凭本地身份领取匿名投票凭证")
    creds = {}
    for code in ("M01", "M02", "M03", "M04"):
        r = client.post(
            f"/me/ballots/{ballot_id}/credential",
            headers={"X-Member-Token": members[code]["token"]},
        )
        creds[code] = r.json()["credential"]
        show(f"{code} 领取凭证", r)
    r = client.post(
        f"/me/ballots/{ballot_id}/credential",
        headers={"X-Member-Token": late["token"]},
    )
    show("范围外会员 M05 领票被拒", r)

    banner("4. 匿名投票（接口只认凭证，不接收身份信息）")
    for code, choice in [("M01", YES), ("M02", NO), ("M03", ABSTAIN), ("M04", YES)]:
        show(f"{code} 投 {choice}", client.post(
            f"/ballots/{ballot_id}/vote",
            json={"credential": creds[code], "choice": choice},
        ))

    banner("5. M01 改投：截止只计每人最后一次选择")
    show("M01 改投反对", client.post(
        f"/ballots/{ballot_id}/vote",
        json={"credential": creds["M01"], "choice": NO},
    ))

    banner("6. 管理员暂停 M02 资格：旧票移除、停权期间不能领票/投票")
    show("暂停 M02", client.post(
        f"/admin/ballots/{ballot_id}/eligibility",
        headers=admin_h,
        json={"member_code": "M02", "action": "suspend"},
    ))
    show("M02 停权期间领票被拒", client.post(
        f"/me/ballots/{ballot_id}/credential",
        headers={"X-Member-Token": members["M02"]["token"]},
    ))
    show("M02 停权期间投票被拒", client.post(
        f"/ballots/{ballot_id}/vote",
        json={"credential": creds["M02"], "choice": YES},
    ))

    banner("7. 管理员名册视图：看得到资格与领票状态，看不到任何选择")
    show("资格名册（无 choice 字段）", client.get(
        f"/admin/ballots/{ballot_id}/eligibility", headers=admin_h
    ))

    banner("8. 恢复 M02：旧票不恢复，必须重新领票并投票")
    show("恢复 M02", client.post(
        f"/admin/ballots/{ballot_id}/eligibility",
        headers=admin_h,
        json={"member_code": "M02", "action": "restore"},
    ))
    r = client.post(
        f"/me/ballots/{ballot_id}/credential",
        headers={"X-Member-Token": members["M02"]["token"]},
    )
    creds["M02"] = r.json()["credential"]
    show("M02 重新领票（代号不变、旧凭证已作废）", r)
    show("M02 重新投赞成", client.post(
        f"/ballots/{ballot_id}/vote",
        json={"credential": creds["M02"], "choice": YES},
    ))

    banner("9. 开放期公开端：只有状态，没有任何计数")
    show("公开端（结算前）", client.get(f"/public/ballots/{ballot_id}"))

    banner("10. 正式结算：只公开三项总数")
    show("结算", client.post(f"/admin/ballots/{ballot_id}/settle", headers=admin_h))
    show("公开端（结算后）", client.get(f"/public/ballots/{ballot_id}"))

    banner("11. 结算固化：之后的资格变化与投票都不影响结果")
    show("结算后停权被拒", client.post(
        f"/admin/ballots/{ballot_id}/eligibility",
        headers=admin_h,
        json={"member_code": "M01", "action": "suspend"},
    ))
    show("结算后投票被拒", client.post(
        f"/ballots/{ballot_id}/vote",
        json={"credential": creds["M01"], "choice": YES},
    ))
    show("公开结果保持不变", client.get(f"/public/ballots/{ballot_id}"))

    print("\n演示完成。预期最终结果：赞成 2（M02 重投、M04）、"
          "反对 1（M01 改投）、弃权 1（M03）。")


if __name__ == "__main__":
    main()
