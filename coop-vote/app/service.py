"""业务服务层：所有状态变更与权限边界集中于此。

约定：
- 路由只做鉴权和参数校验，具体能否操作由本层按数据库当前状态决定。
- 每个写操作在一个 ``BEGIN IMMEDIATE`` 事务内完成，并发时按获取锁
  （即服务端实际接受）的顺序串行生效。
- 业务冲突统一抛 ServiceError，由 FastAPI 映射为 HTTP 状态码。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .database import read, tx
from .security import generate_token, hash_credential, hash_member_token

CHOICES = ("yes", "no", "abstain")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------- 会员管理
def create_member(db_path: str, salt: str, code: str, name: str, is_admin: bool = False) -> dict[str, Any]:
    raw_token = generate_token()
    now = utc_now()
    with tx(db_path) as conn:
        try:
            cur = conn.execute(
                "INSERT INTO members(code, name, token_hash, is_admin, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (code, name, hash_member_token(raw_token, salt), 1 if is_admin else 0, now),
            )
        except sqlite3.IntegrityError:
            raise ServiceError(409, f"会员编号已存在: {code}")
        member_id = cur.lastrowid
    return {"id": member_id, "code": code, "name": name, "is_admin": is_admin, "token": raw_token}


# ---------------------------------------------------------------- 表决管理
def create_ballot(db_path: str, title: str) -> dict[str, Any]:
    with tx(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO ballots(title, status, created_at) VALUES (?, 'draft', ?)",
            (title, utc_now()),
        )
        ballot_id = cur.lastrowid
    return get_ballot(db_path, ballot_id)


def open_ballot(db_path: str, ballot_id: int) -> dict[str, Any]:
    """开启表决：冻结当前全部会员的资格范围，并为每人签发匿名代号。"""
    now = utc_now()
    with tx(db_path) as conn:
        ballot = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        if ballot is None:
            raise ServiceError(404, "表决不存在")
        if ballot["status"] != "draft":
            raise ServiceError(409, f"表决当前状态为 {ballot['status']}，不能开启")
        members = conn.execute("SELECT id FROM members ORDER BY id").fetchall()
        for m in members:
            cur = conn.execute(
                "INSERT INTO eligibilities(ballot_id, member_id, status, epoch, frozen_at) "
                "VALUES (?, ?, 'active', 1, ?)",
                (ballot_id, m["id"], now),
            )
            eligibility_id = cur.lastrowid
            conn.execute(
                "INSERT INTO pseudonyms(ballot_id, eligibility_id, voter_pid, created_at) "
                "VALUES (?, ?, ?, ?)",
                (ballot_id, eligibility_id, f"pid_{generate_token(16)}", now),
            )
        conn.execute(
            "UPDATE ballots SET status='open', opened_at=? WHERE id=?",
            (now, ballot_id),
        )
    return get_ballot(db_path, ballot_id)


def settle_ballot(db_path: str, ballot_id: int) -> dict[str, Any]:
    """正式结算：统计当前仍有效（active）的每人最后一票并固化总数。"""
    now = utc_now()
    with tx(db_path) as conn:
        ballot = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        if ballot is None:
            raise ServiceError(404, "表决不存在")
        if ballot["status"] != "open":
            raise ServiceError(409, f"表决当前状态为 {ballot['status']}，不能结算")
        rows = conn.execute(
            "SELECT v.choice AS choice, COUNT(*) AS n "
            "FROM votes v "
            "JOIN pseudonyms p ON p.id = v.pseudonym_id "
            "JOIN eligibilities e ON e.id = p.eligibility_id "
            "WHERE v.ballot_id=? AND e.status='active' "
            "GROUP BY v.choice",
            (ballot_id,),
        ).fetchall()
        totals = {c: 0 for c in CHOICES}
        for r in rows:
            totals[r["choice"]] = r["n"]
        conn.execute(
            "INSERT INTO results(ballot_id, yes_count, no_count, abstain_count, settled_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ballot_id, totals["yes"], totals["no"], totals["abstain"], now),
        )
        # 决议：只有赞成票多于反对票才通过；绑定的提案在同一事务内执行，
        # 保证“票数固化 + 拨款/退款”原子生效，且同一决议不会重复执行。
        from . import treasury

        approved = totals["yes"] > totals["no"]
        treasury.apply_proposal_decision(conn, ballot_id, approved, now)
        conn.execute(
            "UPDATE ballots SET status='settled', settled_at=? WHERE id=?",
            (now, ballot_id),
        )
    return get_public_ballot(db_path, ballot_id)


def get_ballot(db_path: str, ballot_id: int) -> dict[str, Any]:
    with read(db_path) as conn:
        row = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        if row is None:
            raise ServiceError(404, "表决不存在")
        return dict(row)


def get_public_ballot(db_path: str, ballot_id: int) -> dict[str, Any]:
    """公开端：结算前只给元数据与状态；结算后只给三项总数。"""
    with read(db_path) as conn:
        row = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        if row is None:
            raise ServiceError(404, "表决不存在")
        out = {
            "ballot_id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "results": None,
        }
        if row["status"] == "settled":
            res = conn.execute(
                "SELECT * FROM results WHERE ballot_id=?", (ballot_id,)
            ).fetchone()
            out["results"] = {
                "yes": res["yes_count"],
                "no": res["no_count"],
                "abstain": res["abstain_count"],
            }
        return out


# ---------------------------------------------------------------- 凭证与投票
def issue_credential(db_path: str, salt: str, ballot_id: int, member_id: int) -> dict[str, Any]:
    """签发/重领投票凭证。

    - 仅开放中、且资格处于 active 的会员可领。
    - 重领不作废资格，只把旧凭证置为失效；匿名代号保持不变，
      因此同一资格期内重领不会产生第二份独立计票资格。
    """
    now = utc_now()
    raw_credential = generate_token()
    with tx(db_path) as conn:
        ballot = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        if ballot is None:
            raise ServiceError(404, "表决不存在")
        if ballot["status"] != "open":
            raise ServiceError(409, "表决不在开放期，无法领取凭证")
        elig = conn.execute(
            "SELECT e.*, p.voter_pid AS voter_pid "
            "FROM eligibilities e "
            "JOIN pseudonyms p ON p.eligibility_id = e.id "
            "WHERE e.ballot_id=? AND e.member_id=?",
            (ballot_id, member_id),
        ).fetchone()
        if elig is None:
            raise ServiceError(403, "您不在本次表决的冻结会员范围内")
        if elig["status"] != "active":
            raise ServiceError(403, "会员资格已被暂停，不能领取凭证")
        prior = conn.execute(
            "SELECT COUNT(*) AS n FROM credentials WHERE eligibility_id=? AND active=1",
            (elig["id"],),
        ).fetchone()
        reissued = prior["n"] > 0
        # 同一资格的旧凭证一律失效，防止两份凭证并行使用
        conn.execute(
            "UPDATE credentials SET active=0 WHERE eligibility_id=?",
            (elig["id"],),
        )
        conn.execute(
            "INSERT INTO credentials(ballot_id, eligibility_id, token_hash, active, issued_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (ballot_id, elig["id"], hash_credential(raw_credential, salt), now),
        )
        return {
            "ballot_id": ballot_id,
            "voter_pid": elig["voter_pid"],
            "credential": raw_credential,
            "reissued": reissued,
        }


def cast_vote(db_path: str, salt: str, ballot_id: int, credential: str, choice: str) -> dict[str, Any]:
    """凭投票凭证投票或改投。

    - 接口只接收凭证，不接收任何会员身份字段，返回也不含会员信息。
    - 停权期间凭证无法通过资格校验；停权时旧票已被删除，恢复后需重投。
    - 改投为同代号行的覆盖更新，截止只保留最后一次有效选择。
    """
    if choice not in CHOICES:
        raise ServiceError(422, f"无效选项，允许: {', '.join(CHOICES)}")
    now = utc_now()
    token_hash = hash_credential(credential, salt)
    with tx(db_path) as conn:
        cred = conn.execute(
            "SELECT * FROM credentials WHERE token_hash=?", (token_hash,)
        ).fetchone()
        if cred is None or not cred["active"]:
            raise ServiceError(401, "凭证无效或已作废")
        if cred["ballot_id"] != ballot_id:
            raise ServiceError(403, "凭证不属于本次表决")
        ballot = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        if ballot["status"] != "open":
            raise ServiceError(409, "表决已关闭，不能投票")
        elig = conn.execute(
            "SELECT e.*, p.id AS pseudonym_id, p.voter_pid AS voter_pid "
            "FROM eligibilities e "
            "JOIN pseudonyms p ON p.eligibility_id = e.id "
            "WHERE e.id=?",
            (cred["eligibility_id"],),
        ).fetchone()
        if elig["status"] != "active":
            raise ServiceError(403, "会员资格已被暂停，不能投票")
        existing = conn.execute(
            "SELECT id FROM votes WHERE pseudonym_id=?", (elig["pseudonym_id"],)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO votes(ballot_id, pseudonym_id, choice, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (ballot_id, elig["pseudonym_id"], choice, now),
            )
            changed = True
        else:
            conn.execute(
                "UPDATE votes SET choice=?, updated_at=? WHERE pseudonym_id=?",
                (choice, now, elig["pseudonym_id"]),
            )
            changed = True
        return {
            "ballot_id": ballot_id,
            "voter_pid": elig["voter_pid"],
            "choice": choice,
            "updated_at": now,
            "changed": changed,
        }


# ---------------------------------------------------------------- 资格管理
def set_eligibility(db_path: str, ballot_id: int, member_code: str, suspend: bool) -> dict[str, Any]:
    """管理员暂停/恢复范围内某位会员的资格（幂等）。"""
    now = utc_now()
    with tx(db_path) as conn:
        ballot = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        if ballot is None:
            raise ServiceError(404, "表决不存在")
        if ballot["status"] == "settled":
            raise ServiceError(409, "表决已结算，资格状态不可再变更")
        member = conn.execute("SELECT * FROM members WHERE code=?", (member_code,)).fetchone()
        if member is None:
            raise ServiceError(404, f"会员不存在: {member_code}")
        elig = conn.execute(
            "SELECT e.*, p.id AS pseudonym_id "
            "FROM eligibilities e "
            "JOIN pseudonyms p ON p.eligibility_id = e.id "
            "WHERE e.ballot_id=? AND e.member_id=?",
            (ballot_id, member["id"]),
        ).fetchone()
        if elig is None:
            raise ServiceError(404, "该会员不在本次表决的冻结范围内")
        if suspend:
            if elig["status"] != "suspended":
                conn.execute(
                    "UPDATE eligibilities SET status='suspended', epoch=epoch+1 WHERE id=?",
                    (elig["id"],),
                )
                # 已投票暂不计入：直接移除旧票，恢复后不自动恢复、必须重投
                conn.execute("DELETE FROM votes WHERE pseudonym_id=?", (elig["pseudonym_id"],))
            new_status = "suspended"
        else:
            if elig["status"] == "suspended":
                conn.execute(
                    "UPDATE eligibilities SET status='active', epoch=epoch+1 WHERE id=?",
                    (elig["id"],),
                )
            new_status = "active"
    return {
        "ballot_id": ballot_id,
        "member_code": member_code,
        "status": new_status,
        "note": "停权前选票已作废，恢复后需重新投票" if suspend else "资格已恢复，旧票不恢复，请重新投票",
        "at": now,
    }


def list_eligibilities(db_path: str, ballot_id: int) -> list[dict[str, Any]]:
    """名册管理员视角：只能看资格状态与是否已领凭证，看不到任何选择。"""
    with read(db_path) as conn:
        ballot = conn.execute("SELECT id FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        if ballot is None:
            raise ServiceError(404, "表决不存在")
        rows = conn.execute(
            """
            SELECT m.code AS code,
                   m.name AS name,
                   e.status AS status,
                   e.epoch AS epoch,
                   EXISTS (
                       SELECT 1 FROM credentials c
                       WHERE c.eligibility_id = e.id AND c.active = 1
                     ) AS has_credential
            FROM eligibilities e
            JOIN members m ON m.id = e.member_id
            WHERE e.ballot_id = ?
            ORDER BY m.id
            """,
            (ballot_id,),
        ).fetchall()
        return [dict(r) for r in rows]
