# -*- coding: utf-8 -*-
"""SQLite 三表测试：去重/落库/读取（tmp_path 隔离，不碰真实 db）。"""
import threading


def test_trial_id_deterministic():
    """同 params+snapshot+seed → 同 trial_id（去重键，spec §3.2）。"""
    from discovery.store import trial_id_of
    p = {"window": 80, "x": 1}
    assert trial_id_of(p, "snap1", 42) == trial_id_of(p, "snap1", 42)


def test_trial_id_differs_on_params():
    from discovery.store import trial_id_of
    assert trial_id_of({"window": 80}, "snap1", 42) != trial_id_of({"window": 60}, "snap1", 42)


def test_write_and_read_trial(tmp_path):
    """落 trial + 去重（同 trial_id 不重复写）。"""
    from discovery.store import init_db, connect, write_trial, trial_exists
    db = str(tmp_path / "t.db")
    init_db(db)
    with connect(db) as conn:
        write_trial(conn, "tid1", {"window": 80}, "snap1", "eng1", "holdout_2025_2026",
                    {"ann": 0.7}, {"ann": 1.8}, "manual")
        assert trial_exists(conn, "tid1") is True
        assert trial_exists(conn, "tid_other") is False


def test_concurrent_write_no_deadlock(tmp_path):
    """多线程并发写（WAL + 单点写锁）不锁死——spec §8 拷问②。"""
    from discovery.store import init_db, connect, write_trial
    db = str(tmp_path / "t.db")
    init_db(db)
    errors = []

    def writer(i):
        try:
            with connect(db) as conn:
                write_trial(conn, f"tid{i}", {"window": 80}, "snap1", "eng1",
                            "s", {"ann": 0.1}, {"ann": 0.2}, "t")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM trial").fetchone()["c"]
    assert n == 10


def test_write_snapshot(tmp_path):
    from discovery.store import init_db, connect, write_snapshot
    from discovery.snapshot import SnapshotMeta
    db = str(tmp_path / "t.db")
    init_db(db)
    meta = SnapshotMeta("snap1", "创板科创", 1334, "2025~2026", "2025-01-01")
    with connect(db) as conn:
        write_snapshot(conn, meta)
        row = conn.execute("SELECT * FROM snapshot WHERE snapshot_hash=?", ("snap1",)).fetchone()
    assert row["universe_count"] == 1334


def test_init_db_search_run_migration_idempotent(tmp_path):
    """search_run 表扩跨夜字段后，init_db 重复调用不报错（PRAGMA migration 幂等）。"""
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)   # 首次：建表 + migration 补列
    init_db(db)   # 二次：migration 列已存，不重复 ALTER（不报错）
    init_db(db)   # 三次：再确认幂等
    import sqlite3
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(search_run)")}
    con.close()
    assert {"frontier_size_prev", "k_rounds_no_expansion", "daemon_run_count"} <= cols


def test_write_read_daemon_state_roundtrip(tmp_path):
    """write_search_run → read_latest_search_run → write_daemon_state 往返一致。"""
    from discovery.store import (init_db, connect, write_search_run,
                                 read_latest_search_run, write_daemon_state)
    db = str(tmp_path / "t.db")
    init_db(db)
    snap = "abc123"
    with connect(db) as conn:
        write_search_run(conn, run_id="r1", snapshot_hash=snap, started_at="t1",
                         ended_at="t1e", n_trials=5, status="budget_exhausted",
                         frontier_size=3, k_rounds_no_expansion=0, daemon_run_count=1, note="")
        write_search_run(conn, run_id="r2", snapshot_hash=snap, started_at="t2",
                         ended_at="t2e", n_trials=4, status="budget_exhausted",
                         frontier_size=3, k_rounds_no_expansion=1, daemon_run_count=2, note="")
    with connect(db) as conn:
        latest = read_latest_search_run(conn, snap)
    assert latest is not None
    assert latest["run_id"] == "r2"                  # 最新一行（started_at DESC）
    assert latest["k_rounds_no_expansion"] == 1
    # daemon 更新本次行跨夜状态
    with connect(db) as conn:
        write_daemon_state(conn, run_id="r2", frontier_size=3,
                           k_rounds_no_expansion=2, daemon_run_count=2, status="converged")
        latest2 = read_latest_search_run(conn, snap)
    assert latest2["k_rounds_no_expansion"] == 2
    assert latest2["status"] == "converged"


def test_read_latest_search_run_none_when_empty(tmp_path):
    """首次 daemon（无历史 run）→ read_latest_search_run 返 None（不抛）。"""
    from discovery.store import init_db, connect, read_latest_search_run
    db = str(tmp_path / "t.db")
    init_db(db)
    with connect(db) as conn:
        assert read_latest_search_run(conn, "no_such_snap") is None
