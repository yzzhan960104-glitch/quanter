# -*- coding: utf-8 -*-
"""24h 低功率 discovery 调度测试（2026-08-03）。

物理意图：把"每晚 02:00 集中 4h 高功率"改为"全天低频小批"：每小时触发一次
（避开盘中 09:00-16:59 与 18:00 pipeline/digest 窗口），每轮 1 组、单进程、K 放大
（24 次≈1 天）。trial 去重幂等保证多触发不重跑；跨夜 k 语义在低功率下退化为
"连续 24 次前沿不扩张才收敛"（daemon --k-rounds 24 透传）。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from discovery import daemon
import presentation.server.main as main

_TZ = ZoneInfo("Asia/Shanghai")


def test_low_power_window_allowed_hours():
    """低功率窗口：0-8 与 17、19-23 允许；9-16（盘中）与 18（pipeline/digest）跳过。"""
    ok = {0, 1, 8, 17, 19, 23}
    skip = {9, 10, 12, 15, 16, 18}
    for h in ok:
        assert main._discovery_low_power_allowed(datetime(2026, 8, 3, h, 5, tzinfo=_TZ)) is True
    for h in skip:
        assert main._discovery_low_power_allowed(datetime(2026, 8, 3, h, 5, tzinfo=_TZ)) is False


def test_run_daemon_cycle_uses_budget_groups(tmp_path):
    """budget_groups 直给时不再按小时折算（低功率每轮 1-2 组）。"""
    from discovery.snapshot import SnapshotMeta
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01", data_hash="h")
    seen = {}

    def _rs(snapshot_meta, split, **kw):
        seen.update(kw)
        import types
        s = types.SimpleNamespace(
            run_id="r1", frontier_size=3, status="budget_exhausted", top_trial_id="",
            top_inner_calmar=0.0, rho=0.0, ei=0.0, snapshot_hash="snap1",
            n_new_trials=0, convergence_reason="", daemon_run_count=0)
        from discovery.store import connect as _c, write_search_run as _w
        with _c(db) as conn:
            _w(conn, run_id=s.run_id, snapshot_hash="snap1", started_at="t1",
               ended_at="t1e", n_trials=0, status=s.status, frontier_size=3,
               k_rounds_no_expansion=0, daemon_run_count=0, note="")
        return s

    daemon.run_daemon_cycle(meta, None, db, run_search_fn=_rs,
                            budget_hours=4, budget_groups=1, K=24)
    assert seen["budget"] == 1


def test_run_research_digest_push_low_power_command(monkeypatch):
    """低功率子进程带 --budget-groups 1 --n-proc 1 --k-rounds 24（小批慢跑参数）。"""
    calls = []

    class _FakePopen:
        def __init__(self, args, **kw):
            calls.append(args)

    monkeypatch.setattr(main._subprocess, "Popen", _FakePopen)
    # 时间注入隔离：_run_discovery_subprocess 内部调 _discovery_low_power_allowed
    # (datetime.now())，真实墙钟落在 9-16/18 点窗口会 return 不调 Popen → calls 空 →
    # IndexError。本测试聚焦 low_power 参数构造，不应依赖跑测时刻，故强制窗口放行。
    monkeypatch.setattr(main, "_discovery_low_power_allowed", lambda now=None: True)
    main._run_discovery_subprocess(low_power=True)
    assert calls[0][1:4] == ["-m", "discovery", "daemon"]
    assert calls[0][4:] == ["--budget-groups", "1", "--n-proc", "1", "--k-rounds", "24",
                            "--no-eval-replay-top", "--tpe-trials", "0"]
