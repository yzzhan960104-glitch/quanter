# -*- coding: utf-8 -*-
"""optuna TPE 序贯优化测试：warm start + 序贯集中 + EI + 可复现。
合成 objective_fn（不真实 evaluate，避免读 parquet）；真实 TPE 跑批集成在 Task 8 slow。
"""


def _toy_space():
    return [("window", [40, 60, 80])]


def test_tpe_search_returns_seed_plus_tpe_trials():
    """tpe_search 返回 (seed + tpe) 所有 trial params + study。"""
    from discovery.search import tpe_search
    obj = lambda p: p["window"] / 100.0   # window 越大 calmar 越高（合成）
    params, study = tpe_search([{"window": 40}], obj, n_trials=5, seed=42,
                               param_space=_toy_space())
    assert len(params) == 6               # 1 seed + 5 tpe
    assert all("window" in p for p in params)


def test_tpe_search_warm_start_seed_included():
    """warm start：enqueue 的 seed params 在 study trials 里。"""
    from discovery.search import tpe_search
    obj = lambda p: p["window"] / 100.0
    params, _ = tpe_search([{"window": 80}, {"window": 60}], obj, n_trials=3,
                           seed=42, param_space=_toy_space())
    assert {"window": 80} in params and {"window": 60} in params


def test_tpe_search_finds_high_calmar():
    """TPE 序贯应探索到高 calmar 区（best_value >= 0.5，即 60 或 80 档）。"""
    from discovery.search import tpe_search
    obj = lambda p: p["window"] / 100.0   # 最优 window=80 → 0.8
    _, study = tpe_search([{"window": 40}], obj, n_trials=20, seed=42,
                          param_space=_toy_space())
    assert study.best_value >= 0.5        # TPE 前 10 startup 随机覆盖 3 档，必探到 60/80


def test_tpe_search_reproducible():
    """同 seed 同输入 → 同 best_value（TPESampler(seed=) 可复现）。"""
    from discovery.search import tpe_search
    obj = lambda p: p["window"] / 100.0
    _, s1 = tpe_search([{"window": 40}], obj, n_trials=8, seed=7, param_space=_toy_space())
    _, s2 = tpe_search([{"window": 40}], obj, n_trials=8, seed=7, param_space=_toy_space())
    assert s1.best_value == s2.best_value


def test_expected_improvement_zero_when_stalled():
    """判据②代理：最近 window best 无改进 → EI=0。"""
    from discovery.search import expected_improvement
    class FT:
        def __init__(self, v): self.value = v
    class FS:
        def __init__(self, vs): self.trials = [FT(v) for v in vs]
    # 前 5 轮爬升到 0.9，后 5 轮全 0.9（最近 window=5 无改进）
    s = FS([0.1, 0.3, 0.5, 0.7, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9])
    assert expected_improvement(s, window=5) == 0.0


def test_expected_improvement_positive_when_growing():
    """最近 window best 仍改进 → EI>0。"""
    from discovery.search import expected_improvement
    class FT:
        def __init__(self, v): self.value = v
    class FS:
        def __init__(self, vs): self.trials = [FT(v) for v in vs]
    s = FS([0.1, 0.3, 0.5, 0.7, 0.9])
    assert expected_improvement(s, window=5) > 0.0
