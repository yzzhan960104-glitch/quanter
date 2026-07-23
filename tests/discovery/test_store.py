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
