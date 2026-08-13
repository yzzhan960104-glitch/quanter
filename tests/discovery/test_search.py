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


def test_snap_to_candidates_nearest_numeric():
    """越界数值 snap 到候选档最近邻（0.0→0.05）。"""
    from discovery.search import _snap_to_candidates
    space = [("trailing_step", [0.05, 0.1, 0.15])]
    assert _snap_to_candidates({"trailing_step": 0.0}, space)["trailing_step"] == 0.05
    assert _snap_to_candidates({"trailing_step": 0.12}, space)["trailing_step"] == 0.1


def test_snap_to_candidates_passthrough_in_range():
    """候选档内的值原样返回。"""
    from discovery.search import _snap_to_candidates
    space = [("window", [40, 60, 80])]
    assert _snap_to_candidates({"window": 60}, space)["window"] == 60


def test_tpe_search_enqueues_snapped_seed_no_crash():
    """normalize 越界 seed（trailing_step=0.0）enqueue 不 crash（snap 兜底）。"""
    from discovery.search import tpe_search
    space = [("trailing_step", [0.05, 0.1, 0.15])]
    obj = lambda p: p["trailing_step"]
    params, study = tpe_search([{"trailing_step": 0.0}], obj, n_trials=3, seed=42, param_space=space)
    assert len(params) == 4   # 1 seed(snapped) + 3 tpe，不 crash


# ============================================================================
# P2（2026-08-13 · spec §3）：tpe_search_batch 批量 ask/tell
# ============================================================================
def _peak_objective(params):
    """合成峰形 objective：window 越接近 80 越好（峰在候选档 80），其余维无信息。

    用于验证 TPE 收敛方向（不依赖真实数据湖），与 param_space 无耦合。
    """
    return float(abs(80 - params.get("window", 40)) <= 0) * 5.0 + \
        max(0.0, 2.0 - abs(params.get("window", 40) - 80) / 20.0)


def test_tpe_search_batch_finds_peak():
    """批量 ask/tell 在合成峰形 objective 上收敛到峰邻域（TPE 学习生效）。"""
    from discovery.search import tpe_search_batch
    space = [("window", [40, 60, 80]), ("min_rr", [1.0, 1.5, 2.0])]
    seeds = [{"window": 40, "min_rr": 2.0}, {"window": 60, "min_rr": 1.0}]
    values = [0.0, 1.0]

    def _eval(plist):
        return [{"inner": {"calmar": _peak_objective(p)}} for p in plist]

    new_pairs, study = tpe_search_batch(seeds, values, _eval, n_trials=24,
                                        seed=7, param_space=space, batch_size=6)
    assert len(new_pairs) == 24
    # 峰 = window=80（calmar 5.0）；TPE 应找得到并成为 best
    assert study.best_value >= 5.0 - 1e-9
    assert any(p[0]["window"] == 80 for p in new_pairs)


def test_tpe_search_batch_matches_serial_trend():
    """同 seed 串行 vs 批量：收敛趋势一致（best_value 同量级逼近真峰，非逐位同）。

    P2 验收项（spec §3）：批量模式改变采样顺序（K 个 ask 后统一 tell vs 逐 tell），
    TPE 后验更新节奏不同 → 具体候选不同，但都应收敛到峰。二者 best 与真峰的差同量级。
    """
    from discovery.search import tpe_search_batch, tpe_search
    space = [("window", [40, 60, 80]), ("min_rr", [1.0, 1.5, 2.0])]
    seeds = [{"window": 40, "min_rr": 2.0}, {"window": 60, "min_rr": 1.0}]

    _, study_serial = tpe_search(seeds, _peak_objective, n_trials=24, seed=7,
                                 param_space=space)
    _, study_batch = tpe_search_batch(seeds, [_peak_objective(p) for p in seeds],
                                      lambda plist: [{"inner": {"calmar": _peak_objective(p)}}
                                                     for p in plist],
                                      n_trials=24, seed=7, param_space=space, batch_size=6)
    peak = 5.0
    serial_gap = peak - study_serial.best_value
    batch_gap = peak - study_batch.best_value
    assert serial_gap <= 1e-6 and batch_gap <= 1e-6, (
        f"串行 gap={serial_gap} 批量 gap={batch_gap}——两者都应逼近峰（gap≈0）")


def test_tpe_search_batch_failed_eval_marks_failed():
    """评估失败组：new_pairs 含 (params, None) + study 该 trial FAILED 态（不毒化后验）。"""
    from discovery.search import tpe_search_batch
    space = [("window", [40, 60, 80]), ("min_rr", [1.0, 1.5, 2.0])]
    seeds = [{"window": 40, "min_rr": 2.0}]

    def _eval(plist):
        # 全部失败：返回 None 组
        return [None for _ in plist]

    new_pairs, study = tpe_search_batch(seeds, [0.0], _eval, n_trials=4,
                                        seed=3, param_space=space, batch_size=4)
    assert len(new_pairs) == 4
    assert all(res is None for _, res in new_pairs)
    failed = [t for t in study.trials if t.state.name == "FAIL"]
    assert len(failed) == 4
