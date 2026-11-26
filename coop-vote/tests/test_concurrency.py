"""并发测试：关闭、改投、停权以服务端实际接受顺序为准。

直接在服务层用多线程压同一 SQLite 文件，所有写事务由
``database.tx`` 的全局锁 + BEGIN IMMEDIATE 串行化，
因此并发结果必须等价于某个确定的接受顺序。
"""
from __future__ import annotations

import threading

from app import service
from app.database import read

from .conftest import create_member, issue, make_open_ballot


def _run_concurrently(*fns):
    results, errors = _run_collect(*fns)
    assert not errors, errors
    return results


def _run_collect(*fns):
    """并发执行，返回 (正常返回列表, 异常列表)，不预设异常是否合法。"""
    barrier = threading.Barrier(len(fns))
    results: list = []
    errors: list = []

    def worker(fn):
        barrier.wait()
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 - 并发顺序测试中异常是合法结果
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def _vote_count(db_path: str, ballot_id: int) -> dict[str, int]:
    with read(db_path) as conn:
        rows = conn.execute(
            "SELECT choice, COUNT(*) AS n FROM votes WHERE ballot_id=? GROUP BY choice",
            (ballot_id,),
        ).fetchall()
    totals = {"yes": 0, "no": 0, "abstain": 0}
    for r in rows:
        totals[r["choice"]] = r["n"]
    return totals


def test_concurrent_revotes_leave_exactly_one_last_choice(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    c = issue(env, ballot_id, m)["credential"]

    fns = [
        lambda ch=ch: service.cast_vote(env.db, env.salt, ballot_id, c, ch)
        for ch in ("yes", "no", "abstain", "yes", "no")
    ]
    _run_concurrently(*fns)

    with read(env.db) as conn:
        rows = conn.execute("SELECT * FROM votes").fetchall()
        assert len(rows) == 1  # 无论如何交错，每人只保留一行
        last = rows[0]["choice"]
    # 最终选择必然是三个合法选项之一，且与公开结算一致
    assert last in ("yes", "no", "abstain")
    service.settle_ballot(env.db, ballot_id)
    public = service.get_public_ballot(env.db, ballot_id)
    assert public["results"][last] == 1


def test_concurrent_suspend_vs_vote_ordering_is_well_defined(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    c = issue(env, ballot_id, m)["credential"]

    # 重复多轮，两种接受顺序都可能出现，但每轮结果必须自洽
    for _ in range(5):
        # 每轮恢复后重新领票，保证起始条件一致
        service.set_eligibility(env.db, ballot_id, "M01", suspend=False)
        c = issue(env, ballot_id, m)["credential"]

        def do_vote():
            service.cast_vote(env.db, env.salt, ballot_id, c, "yes")

        def do_suspend():
            service.set_eligibility(env.db, ballot_id, "M01", suspend=True)

        _, errors = _run_collect(do_vote, do_suspend)

        with read(env.db) as conn:
            status = conn.execute(
                "SELECT status FROM eligibilities WHERE ballot_id=? AND member_id=?",
                (ballot_id, m["id"]),
            ).fetchone()["status"]
            vote_rows = conn.execute("SELECT COUNT(*) AS n FROM votes").fetchone()["n"]

        assert status == "suspended"
        if errors:
            # 停权先被接受：投票被 403 拒绝，无票落库
            assert len(errors) == 1
            assert errors[0].status_code == 403
            assert vote_rows == 0
        else:
            # 投票先被接受、停权后到：停权移除该票，最终仍无票
            assert vote_rows == 0
        # 无论顺序如何，停权状态下都不含该会员的票
        assert _vote_count(env.db, ballot_id) == {"yes": 0, "no": 0, "abstain": 0}


def test_concurrent_settle_vs_vote_has_single_winning_order(env):
    m = create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)
    c = issue(env, ballot_id, m)["credential"]

    def do_vote():
        service.cast_vote(env.db, env.salt, ballot_id, c, "yes")

    def do_settle():
        service.settle_ballot(env.db, ballot_id)

    _, errors = _run_collect(do_vote, do_settle)

    public = service.get_public_ballot(env.db, ballot_id)
    assert public["status"] == "settled"
    vote_errors = [e for e in errors if e.status_code == 409]
    if vote_errors:
        # 结算先被接受：投票被拒，结果为全零
        assert len(errors) == 1
        assert public["results"] == {"yes": 0, "no": 0, "abstain": 0}
    else:
        # 投票先被接受：计入 1 票，结算正常完成
        assert not errors
        assert public["results"] == {"yes": 1, "no": 0, "abstain": 0}


def test_repeated_concurrent_suspend_restore_converges(env):
    create_member(env, "M01", "张三")
    ballot_id = make_open_ballot(env)

    fns = [
        lambda: service.set_eligibility(env.db, ballot_id, "M01", suspend=s)
        for s in (True, False, True, False, True)
    ]
    _run_concurrently(*fns)

    with read(env.db) as conn:
        row = conn.execute(
            "SELECT e.status AS status, e.epoch AS epoch "
            "FROM eligibilities e JOIN members m ON m.id=e.member_id "
            "WHERE e.ballot_id=? AND m.code='M01'",
            (ballot_id,),
        ).fetchone()
    # 最终必然是两个合法状态之一，且全程无异常、无损坏
    assert row["status"] in ("active", "suspended")
    assert row["epoch"] >= 1
