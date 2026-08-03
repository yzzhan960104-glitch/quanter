# -*- coding: utf-8 -*-
"""跑批调度测试：断点续跑去重 + 采样落库计数 + 汇总字段。

合成 universe 快测（monkeypatch freeze/eval_batch 避免真实 Pool+parquet）；
真实跑批集成在 Task 5 cli slow 测试。
"""
from datetime import date


def _fake_meta():
    from discovery.snapshot import SnapshotMeta
    return SnapshotMeta("snaptest", "创板科创", 1, "2025~2026", "2025-01-01")


def _fake_split():
    from discovery.split import HoldoutSplit, Segment
    return HoldoutSplit(Segment("i", date(2025, 1, 1), date(2025, 12, 31)),
                        Segment("o", date(2026, 1, 1), date(2026, 12, 31)), 5)


def test_persist_trial_writes_and_returns_id(tmp_path, monkeypatch):
    """_persist_trial 落库返回 trial_id（新 trial）。"""
    from discovery import runner
    from discovery.store import init_db, connect, trial_exists
    db = str(tmp_path / "t.db")
    init_db(db)
    params = {"window": 80, "min_rr": 2.0}
    result = {"inner": {"ann": 0.5, "calmar": 2.0, "max_dd": 0.2, "n": 100},
              "outer": {"ann": 1.5, "calmar": 5.0, "max_dd": 0.3, "n": 80}, "n_total": 180}
    with connect(db) as conn:
        tid = runner._persist_trial(conn, params, _fake_meta(), "holdout_2025_2026",
                                    "eng1", result, "sobol", seed=0)
    assert tid is not None
    with connect(db) as conn:
        assert trial_exists(conn, tid) is True


def test_persist_trial_returns_none_on_dup(tmp_path):
    """同 params+snapshot+seed 已存在 → 返回 None（断点续跑去重）。"""
    from discovery import runner
    from discovery.store import init_db, connect
    db = str(tmp_path / "t.db")
    init_db(db)
    params = {"window": 80}
    result = {"inner": {"ann": 0.5}, "outer": {"ann": 1.5}, "n_total": 10}
    with connect(db) as conn:
        tid1 = runner._persist_trial(conn, params, _fake_meta(), "s", "e", result, "sobol", 0)
        tid2 = runner._persist_trial(conn, params, _fake_meta(), "s", "e", result, "sobol", 0)
    assert tid1 is not None
    assert tid2 is None   # 重复落库被 INSERT OR IGNORE 吞


def test_run_search_dedup_on_restart(tmp_path, monkeypatch):
    """kill 重启场景：已落 trial 不重跑（eval_batch 不再被调用于已存组）。"""
    from discovery import runner
    from discovery.store import init_db, connect
    db = str(tmp_path / "t.db")
    init_db(db)

    # monkeypatch sample_search 产固定 3 组（可复现 seed）
    fixed_params = [
        {"window": 40, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0},
        {"window": 60, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0},
        {"window": 80, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0},
    ]
    monkeypatch.setattr(runner, "sample_search", lambda **kw: fixed_params)
    # monkeypatch eval_batch 直接造结果（不真实起 Pool）
    def fake_eval(plist, **kw):
        return [(p, {"inner": {"ann": 0.5, "calmar": 2.0, "max_dd": 0.2, "n": 100},
                     "outer": {"ann": 1.5, "calmar": 5.0, "max_dd": 0.3, "n": 80},
                     "n_total": 180}) for p in plist]
    monkeypatch.setattr(runner, "eval_batch", fake_eval)
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")

    # 第一次跑：3 组全落库
    s1 = runner.run_search(_fake_meta(), _fake_split(), budget=3, n_sobol=2, n_random=1,
                           seed=42, db_path=db)
    assert s1.n_new_trials == 3
    assert s1.n_skipped_dup == 0
    # 第二次跑（模拟重启，同 seed 同采样）：3 组已存，全跳过
    s2 = runner.run_search(_fake_meta(), _fake_split(), budget=3, n_sobol=2, n_random=1,
                           seed=42, db_path=db)
    assert s2.n_new_trials == 0
    assert s2.n_skipped_dup == 3


def test_run_search_summary_fields(tmp_path, monkeypatch):
    """RunSummary 字段齐全（n_sampled/n_evaluated/n_new/n_skipped/n_failed/top_inner_calmar）。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    monkeypatch.setattr(runner, "sample_search", lambda **kw: [
        {"window": 80, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0}])
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [
        (plist[0], {"inner": {"ann": 0.5, "calmar": 3.5, "max_dd": 0.2, "n": 100},
                    "outer": {"ann": 1.5, "calmar": 5.0, "max_dd": 0.3, "n": 80}, "n_total": 180})])
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)
    assert s.n_sampled == 1
    assert s.n_evaluated == 1
    assert s.n_new_trials == 1
    assert s.n_failed == 0
    assert s.top_inner_calmar == 3.5
    assert s.db_path == db
    assert s.status == "budget_exhausted"


def test_run_search_eval_replay_top_fills_replay_metrics(tmp_path, monkeypatch):
    """P1-2（2026-08-03）：eval_replay_top=True 时冠军补跑 replay 口径（主回测同源）。

    物理意图：搜索排序用 kelly/calmar 近似口径，与主回测/实盘 PositionModel 口径
    不同源。冠军额外用 evaluate_replay（backtest.replay 引擎）复评 inner 段，
    产出可对拍的 replay 口径指标（n_hits/win_rate/avg_rr/max_drawdown/annualized）。
    """
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    monkeypatch.setattr(runner, "sample_search", lambda **kw: [
        {"window": 80, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1,
         "trailing_floor": 0.0}])
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [
        (plist[0], {"inner": {"ann": 0.5, "calmar": 3.5, "max_dd": 0.2, "n": 100},
                    "outer": {"ann": 1.5, "calmar": 5.0, "max_dd": 0.3, "n": 80},
                    "n_total": 180})])
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")

    fake_replay = {
        "inner": {"n_hits": 88, "win_rate": 0.55, "avg_rr": 1.8,
                  "max_drawdown": -0.12, "annualized_return": 0.3,
                  "n_trading_days": 240},
        "outer": {}, "n_total": 88, "report": {},
    }
    monkeypatch.setattr(runner, "evaluate_replay",
                        lambda params, universe, split: fake_replay)
    monkeypatch.setattr(runner, "freeze",
                        lambda lake_start="2025-01-01": ({"300001.SZ": 1}, _fake_meta()))

    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db, eval_replay_top=True)
    assert s.top_replay_metrics == fake_replay["inner"]


def test_run_search_eval_replay_top_default_off(tmp_path, monkeypatch):
    """eval_replay_top 默认 False（不额外跑 replay，普通跑批零开销）。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    monkeypatch.setattr(runner, "sample_search", lambda **kw: [
        {"window": 80, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1,
         "trailing_floor": 0.0}])
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [
        (plist[0], {"inner": {"ann": 0.5, "calmar": 3.5, "max_dd": 0.2, "n": 100},
                    "outer": {}, "n_total": 100})])
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    monkeypatch.setattr(runner, "evaluate_replay",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应被调用")))
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)
    assert s.top_replay_metrics == {}


def test_run_search_failed_trial_filtered(tmp_path, monkeypatch):
    """eval_batch 返回 None（异常组）→ n_failed 计数，不落库。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    monkeypatch.setattr(runner, "sample_search", lambda **kw: [
        {"window": 80, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0}])
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [None])  # 全失败
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)
    assert s.n_failed == 1
    assert s.n_new_trials == 0


# ===== Plan 3 Task 6：两阶段搜索 + 收敛自停 + DSR 用例（brief Step 1 verbatim） =====

import json as _json


# 合成评估结果（inner/outer metrics 齐全，供 mock eval/tpe/evaluate 用）
_RES = {"inner": {"ann": 0.5, "calmar": 2.0, "max_dd": 0.2, "n": 100, "sharpe": 1.5},
        "outer": {"ann": 0.5, "calmar": 2.0, "max_dd": 0.2, "n": 80, "sharpe": 1.5}, "n_total": 180}


def _one_param(window=80):
    return {"window": window, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
            "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0}


def _mock_search_deps(monkeypatch, runner, *, sampled, tpe_params=None, tpe_values=None):
    """mock run_search 的外部依赖（sample_search/eval_batch/tpe_search/evaluate/freeze/_engine_hash）。"""
    monkeypatch.setattr(runner, "sample_search", lambda **kw: sampled)
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [(p, _RES) for p in plist])
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    if tpe_params is not None:
        # 构造 fake optuna study（供 expected_improvement 读 .trials[].value）
        class _FT:
            def __init__(self, v): self.value = v
        class _FS:
            def __init__(self, vs): self.trials = [_FT(v) for v in vs]; self.best_value = max(vs) if vs else 0.0
        monkeypatch.setattr(runner, "tpe_search",
                            lambda sp, obj, n_trials, seed, **kw: (tpe_params, _FS(tpe_values or [])))
        monkeypatch.setattr(runner, "evaluate", lambda p, u, s: _RES)
        monkeypatch.setattr(runner, "freeze", lambda lake_start="2025-01-01": ({}, _fake_meta()))


def test_run_search_budget_when_sobol_only(tmp_path, monkeypatch):
    """tpe_trials=0（Sobol-only）→ budget_exhausted（Plan 2 逻辑不破；判据①跨 run 留 daemon）。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    _mock_search_deps(monkeypatch, runner, sampled=[_one_param()])
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)   # tpe_trials 默认 0
    assert s.status == "budget_exhausted"
    assert s.n_new_trials == 1


def test_run_search_converges_with_tpe_low_ei(tmp_path, monkeypatch):
    """tpe_trials>0 + 覆盖达标 + EI<ε → converged（判据④+②命中）。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    sampled = [_one_param(w) for w in (40, 60, 80)]
    _mock_search_deps(monkeypatch, runner, sampled=sampled, tpe_params=sampled,
                      tpe_values=[0.5] * 10)   # 全 0.5 → EI=0（<ε）
    s = runner.run_search(_fake_meta(), _fake_split(), budget=3, n_sobol=3, n_random=0,
                          seed=1, db_path=db, tpe_trials=2, rho_threshold=0.0)   # rho_threshold=0 强制覆盖达标
    assert s.status == "converged"
    assert "ei_below_eps" in s.convergence_reason


def test_run_search_budget_when_coverage_low(tmp_path, monkeypatch):
    """ρ<阈值 → budget_exhausted（判据④前置否决，即便 EI=0 也不自停）。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    _mock_search_deps(monkeypatch, runner, sampled=[_one_param()], tpe_params=[_one_param()],
                      tpe_values=[0.5] * 10)
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db, tpe_trials=1, rho_threshold=0.99)   # ρ 达不到 0.99
    assert s.status == "budget_exhausted"


def test_run_search_dsr_and_frontier_marked(tmp_path, monkeypatch):
    """top-1 算 DSR、Pareto 前沿大小入 RunSummary。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    _mock_search_deps(monkeypatch, runner, sampled=[_one_param()])
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)
    assert 0.0 <= s.dsr_top <= 1.0
    assert s.frontier_size >= 1   # 至少 1 组 trial，自身即前沿
    assert s.rho >= 0.0
