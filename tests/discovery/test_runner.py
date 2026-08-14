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
    """mock run_search 的外部依赖（sample_search/eval_batch/tpe_search_batch/EvalPool/_engine_hash）。

    P2（2026-08-13）：TPE seam 从 tpe_search（串行）迁 tpe_search_batch（ask/tell 批量）+
    worker.EvalPool fake（长驻池语义：eval 返回 (params, _RES) 对齐、close 空操作）。
    """
    monkeypatch.setattr(runner, "sample_search", lambda **kw: sampled)
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [(p, _RES) for p in plist])
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    if tpe_params is not None:
        # 构造 fake optuna study（供 expected_improvement 读 .trials[].value）
        class _FT:
            def __init__(self, v): self.value = v
        class _FS:
            def __init__(self, vs): self.trials = [_FT(v) for v in vs]; self.best_value = max(vs) if vs else 0.0
        # fake 长驻池（runner 阶段二 `from discovery.worker import EvalPool` 引用的是
        # worker 模块名——patch 物理路径 worker.EvalPool）
        class _FakePool:
            def __init__(self, n_proc=None, lake_start="2025-01-01", embargo_days=5):
                pass
            def eval(self, plist):
                return [(p, _RES) for p in plist]
            def close(self):
                pass
        import discovery.worker as _worker_mod
        monkeypatch.setattr(_worker_mod, "EvalPool", _FakePool)
        # tpe_search_batch 签名 (seed_params, seed_values, eval_fn, n_trials, seed, ...)
        # → 返回 ([(params, res), ...], fake_study)
        monkeypatch.setattr(runner, "tpe_search_batch",
                            lambda sp, sv, ef, n_trials, seed, **kw:
                            ([(p, _RES) for p in tpe_params], _FS(tpe_values or [])))
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


def test_run_search_tpe_seeds_filter_none_results(tmp_path, monkeypatch):
    """Critical C1 回归（2026-08-13 外部评审）：results 含 None 时 TPE 阶段不崩。

    旧实现 seed_pairs = results 原样保留 None → `[p for p, _ in seed_pairs]` 解包
    TypeError → daemon 夜跑整夜失败。本测试注入含 None 的 results（去 mock 双层掩盖：
    eval_batch 返回 [None, (params, res)]），断言 run_search 正常完成且 n_failed 计数。
    """
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    p_good = {"window": 80, "min_rr": 2.0}
    monkeypatch.setattr(runner, "sample_search", lambda **kw: [p_good])
    # 双层 mock 拆除：eval_batch 真实返回含 None（第一组退化、第二组成交）
    monkeypatch.setattr(runner, "eval_batch",
                        lambda plist, **kw: [None, (p_good, {
                            "inner": {"ann": 0.5, "calmar": 3.5, "max_dd": 0.2, "n": 100},
                            "outer": {"ann": 1.5, "calmar": 5.0, "max_dd": 0.3, "n": 80},
                            "n_total": 180})])
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    # TPE batch 不 mock：走真 tpe_search_batch（evaluate 回调走 fake pool）
    import discovery.worker as worker_mod
    class _FakePool:
        def __init__(self, n_proc=None, lake_start="2025-01-01", embargo_days=5):
            pass
        def eval(self, plist):
            return [(p, {"inner": {"ann": 0.4, "calmar": 2.0, "max_dd": 0.3, "n": 50},
                         "outer": {"ann": 0.0, "calmar": 0.0, "max_dd": 0.0, "n": 0},
                         "n_total": 50}) for p in plist]
        def close(self):
            pass
    monkeypatch.setattr(worker_mod, "EvalPool", _FakePool)
    s = runner.run_search(_fake_meta(), _fake_split(), budget=2, n_sobol=2, n_random=0,
                          seed=1, db_path=db, tpe_trials=2)
    assert s.n_failed == 1          # 退化组计数
    assert s.n_new_trials == 3      # 1 组成交 search + 2 组 TPE
    assert s.top_inner_calmar == 3.5


def test_run_search_top_ranked_by_min_yearly_calmar(tmp_path, monkeypatch):
    """A2 排序键锁定（spec 计划 Task2 遗留 · 评审 2026-08-15 补齐）：
    DSR 门控排序取 min_yearly_calmar（非整段 calmar）——RunSummary.top_inner_calmar
    须等于各年 min 值，防「TPE 朝 min 优化、top 选择按整段 calmar」目标分裂回退。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    # 整段 calmar=2.0（特化假象）但各年 min=0.5（真实水平）——top 必须取 0.5
    res = {"inner": {"ann": 0.5, "calmar": 2.0, "min_yearly_calmar": 0.5,
                     "max_dd": 0.2, "n": 100, "sharpe": 1.5},
           "outer": {}, "n_total": 100}
    monkeypatch.setattr(runner, "sample_search", lambda **kw: [_one_param()])
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [(p, res) for p in plist])
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    monkeypatch.setattr(runner, "freeze", lambda lake_start="2021-01-01": ({}, _fake_meta()))
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)
    assert s.top_inner_calmar == 0.5, (
        f"top 排序键须为 min_yearly_calmar=0.5，实际 {s.top_inner_calmar}"
        "（若为 2.0 说明排序键回退整段 calmar——目标分裂）")
