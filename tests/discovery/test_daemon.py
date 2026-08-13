# -*- coding: utf-8 -*-
"""L4 守护 daemon 跨夜编排测试（Plan 4 Task 2）。

仅测跨夜编排核心：判据① k 累加/收敛/早退/首次/扩张重置。不测告警/outer（留 T3）。

关键 helper：_make_run_search_fn 模拟真实 run_search 的 write_search_run 副作用
（daemon read_latest_search_run 依赖此行存在，否则跨夜 k 永不累积）——self-review
修过的关键，照 brief verbatim。
"""
import types
from discovery.split import holdout_split


def _fake_summary(run_id, frontier_size, status="budget_exhausted"):
    """造一个最小 RunSummary（避免跑真实 run_search）。"""
    return types.SimpleNamespace(
        run_id=run_id, frontier_size=frontier_size, status=status,
        top_trial_id="t1", top_inner_calmar=1.0, rho=0.9, ei=0.0005,
        snapshot_hash="snap1", n_new_trials=5, convergence_reason="",
    )


def _make_run_search_fn(summaries, db_path, snapshot_hash):
    """假 run_search：按调用序号吐预设 summary，并模拟真实 run_search 的 write_search_run
    副作用（daemon read_latest_search_run 依赖此行存在，否则跨夜 k 永不累积）。"""
    calls = {"i": 0}

    def _rs(*a, **kw):
        i = calls["i"]
        calls["i"] += 1
        s = summaries[i]
        # 副作用：落 search_run 行（真实 run_search 收尾必做的一步，跨夜状态源）。
        # daemon 读 latest → 比对 frontier_size_prev → 决定 k 累加/重置。
        from discovery.store import connect, write_search_run
        with connect(db_path) as conn:
            write_search_run(conn, run_id=s.run_id, snapshot_hash=snapshot_hash,
                             started_at=f"t{i}", ended_at=f"t{i}e", n_trials=5,
                             status=s.status, frontier_size=s.frontier_size,
                             k_rounds_no_expansion=0, daemon_run_count=0, note="")
        return s

    return _rs, calls


def test_daemon_accumulates_k_when_frontier_stagnant(tmp_path):
    """连续 3 夜前沿不扩张 → 第 3 夜 converged_cross=True（跨夜判据①）。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db, connect, write_search_run
    db = str(tmp_path / "t.db")
    init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01")
    split = holdout_split()
    # 4 夜 frontier_size 都=3（不扩张）：夜1 latest=None→k=0 / 夜2→k=1 / 夜3→k=2 / 夜4→k=3 收敛
    sums = [_fake_summary(f"r{i}", 3) for i in range(4)]
    rs_fn, _ = _make_run_search_fn(sums, db, "snap1")
    out = None
    for _ in range(4):
        out = run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, K=3)
    assert out["converged_cross"] is True
    assert out["latest_k"] == 3
    assert out["status"] == "converged"


def test_daemon_resets_k_on_frontier_expansion(tmp_path):
    """前沿扩张（3→5）→ k 重置 0。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01")
    split = holdout_split()
    # 夜1 size=3(k=0), 夜2 size=5 扩张(k=0 重置)
    rs_fn, _ = _make_run_search_fn([_fake_summary("r1", 3), _fake_summary("r2", 5)], db, "snap1")
    run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, K=3)
    out2 = run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, K=3)
    assert out2["latest_k"] == 0
    assert out2["converged_cross"] is False


def test_daemon_first_run_k_zero_when_no_latest(tmp_path):
    """首次 daemon（latest=None）→ k=0，不早退不炸。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01")
    split = holdout_split()
    rs_fn, _ = _make_run_search_fn([_fake_summary("r1", 3)], db, "snap1")
    out = run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, K=3)
    assert out["latest_k"] == 0
    assert out["converged_cross"] is False


def test_daemon_marks_data_changed_when_snapshot_differs(tmp_path):
    """P1-3：上夜 snapshot_hash 与今夜不同（数据增量/复权）→ data_changed=True，k 重置。

    物理意图：每晚数据湖增量落湖 → snapshot_hash 变 → read_latest_search_run 查不到
    上夜记录 → 跨夜判据①静默重置（旧实现无任何提示，k=0/3 原因不可见）。本测试
    锁定 daemon 显式标注 data_changed，让"数据版本变化导致收敛重置"可审计。
    """
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import connect, init_db, write_search_run
    db = str(tmp_path / "t.db")
    init_db(db)
    # 上夜：snap_old（旧数据版本）
    with connect(db) as conn:
        write_search_run(conn, run_id="old_run", snapshot_hash="snap_old",
                         started_at="t0", ended_at="t0e", n_trials=1,
                         status="budget_exhausted", frontier_size=5,
                         k_rounds_no_expansion=2, daemon_run_count=2, note="")
    meta = SnapshotMeta("snap_new", "u", 10, "d2", "2025-01-01", data_hash="h_new")
    split = holdout_split()
    rs_fn, _ = _make_run_search_fn([_fake_summary("r1", 3)], db, "snap_new")
    out = run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, K=3)
    assert out["data_changed"] is True
    assert out["data_hash"] == "h_new"
    assert out["latest_k"] == 0


def test_daemon_data_unchanged_when_same_snapshot(tmp_path):
    """同 snapshot（数据版本一致）→ data_changed=False（跨夜收敛可正常累积）。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01", data_hash="h")
    split = holdout_split()
    rs_fn, _ = _make_run_search_fn([_fake_summary("r1", 3)], db, "snap1")
    out = run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, K=3)
    assert out["data_changed"] is False


def test_daemon_passes_eval_replay_top_to_run_search(tmp_path):
    """P1-2：run_daemon_cycle 透传 eval_replay_top=True（生产默认开，冠军补 replay 口径）。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import connect, init_db, write_search_run
    db = str(tmp_path / "t.db")
    init_db(db)
    # 先落一行上夜记录（避免首次 k 分支干扰断言，专注透传）
    with connect(db) as conn:
        write_search_run(conn, run_id="r0", snapshot_hash="snap1", started_at="t0",
                         ended_at="t0e", n_trials=1, status="budget_exhausted",
                         frontier_size=3, k_rounds_no_expansion=0,
                         daemon_run_count=1, note="")
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01", data_hash="h")
    split = holdout_split()
    seen = {}

    def _rs(*a, **kw):
        seen.update(kw)
        s = _fake_summary("r1", 3)
        from discovery.store import connect as _c, write_search_run as _w
        with _c(db) as conn:
            _w(conn, run_id=s.run_id, snapshot_hash="snap1", started_at="t1",
               ended_at="t1e", n_trials=1, status=s.status,
               frontier_size=s.frontier_size, k_rounds_no_expansion=0,
               daemon_run_count=2, note="")
        return s

    run_daemon_cycle(meta, split, db, run_search_fn=_rs, K=3, eval_replay_top=True)
    assert seen.get("eval_replay_top") is True


def test_daemon_alerts_on_new_champion(tmp_path, monkeypatch):
    """有冠军 → notify_fn 被调（验注入语义：消息含 summary/k/converged_cross/outer）。

    本测试不触达真实钉钉（notify_fn 注入 mock dict 收集器），只验 T2 的注入点把
    正确字段透传给了告警回调——T3 的 _notify_champion 真实实现由 run_daemon 预装。
    """
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01"); split = holdout_split()
    rs_fn, _ = _make_run_search_fn([_fake_summary("r1", 3)], db, "snap1")
    sent = {}
    def _notify(**kw): sent.update(kw)
    run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, notify_fn=_notify)
    assert "summary" in sent and sent["k"] == 0
    assert sent["converged_cross"] is False


def test_daemon_outer_no_feedback(tmp_path, monkeypatch):
    """outer 去偏结果只进返回 dict，不回写 run_search 排序（信息隔离红线 spec §6.2）。

    验证两件事：
      ① eval_outer_fn 被调，返回值原样出现在返回 dict['outer']（供报告/告警消费）；
      ② run_search_fn 收到的调用参数绝不含 outer（run_search 签名无 outer 入参，
         物理上不可能把 outer 回写进排序——这是 inner/outer 隔离的硬保证）。
    """
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01"); split = holdout_split()
    rs_calls = []
    def _rs(*a, **kw): rs_calls.append(kw); return _fake_summary("r1", 3)
    def _eval(tid): return {"ann": 0.5, "calmar": 2.0}
    out = run_daemon_cycle(meta, split, db, run_search_fn=_rs, eval_outer_fn=_eval)
    assert out["outer"] == {"ann": 0.5, "calmar": 2.0}     # outer 进返回 dict
    # run_search 收到的 kwargs 不含 outer（信息隔离：outer 永不回写 run_search 排序）
    assert all("outer" not in kw for kw in rs_calls)


def test_daemon_early_exit_when_converged(tmp_path):
    """latest.status==converged → 早退，不调 run_search。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db, connect, write_search_run
    db = str(tmp_path / "t.db")
    init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01")
    split = holdout_split()
    # 预置一行已收敛的 search_run（latest.status==converged → daemon 早退）
    with connect(db) as conn:
        write_search_run(conn, "prev", "snap1", "t0", "t0e", 5, "converged", 3, 3, 3, "")
    called = {"n": 0}

    def _rs(*a, **kw):
        called["n"] += 1
        return _fake_summary("x", 3)

    out = run_daemon_cycle(meta, split, db, run_search_fn=_rs, K=3)
    assert out["early_exited"] is True
    assert called["n"] == 0    # 未触达 run_search


def test_daemon_calls_auto_publish_with_champion_and_outer(tmp_path):
    """auto_publish_fn 注入：daemon 用冠军 trial_id + outer 调自动 publish 桥。"""
    from discovery.daemon import run_daemon_cycle
    from discovery.snapshot import SnapshotMeta
    from discovery.store import connect, init_db, write_search_run
    db = str(tmp_path / "t.db")
    init_db(db)
    with connect(db) as conn:
        write_search_run(conn, run_id="r0", snapshot_hash="snap1", started_at="t0",
                         ended_at="t0e", n_trials=1, status="budget_exhausted",
                         frontier_size=3, k_rounds_no_expansion=0,
                         daemon_run_count=1, note="")
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01", data_hash="h")
    split = holdout_split()
    calls = {}

    def _rs(*a, **kw):
        s = _fake_summary("r1", 3)
        from discovery.store import connect as _c, write_search_run as _w
        with _c(db) as conn:
            _w(conn, run_id=s.run_id, snapshot_hash="snap1", started_at="t1",
               ended_at="t1e", n_trials=1, status=s.status,
               frontier_size=s.frontier_size, k_rounds_no_expansion=0,
               daemon_run_count=2, note="")
        return s

    def _ap(trial_id, outer):
        calls["trial_id"] = trial_id
        calls["outer"] = outer
        return "exp_auto"

    out = run_daemon_cycle(meta, split, db, run_search_fn=_rs, K=3,
                           auto_publish_fn=_ap,
                           eval_outer_fn=lambda tid: {"ann": 0.12})
    assert calls == {"trial_id": "t1", "outer": {"ann": 0.12}}
    assert out["auto_published_experiment"] == "exp_auto"


def test_integrity_gate_fail_closed():
    """P5-I2 回归（2026-08-13 外部评审）：漏采>0 与扫描失败（None）都拒跑，0 放行。

    fail-open 治理项（评审三、3）：降级方向必须保守——缺信息时收紧而非放开。
    """
    from discovery.daemon import integrity_gate
    ok, reason = integrity_gate(0)
    assert ok and "PASS" in reason
    ok2, reason2 = integrity_gate(3)
    assert not ok2 and "3" in reason2
    ok3, reason3 = integrity_gate(None)
    assert not ok3 and "fail-closed" in reason3
