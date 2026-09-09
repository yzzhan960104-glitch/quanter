# -*- coding: utf-8 -*-
"""覆盖度度量④测试（纯函数，逐维边际覆盖·2026-09-09 修订版）。"""


def test_grid_coverage_grows_with_samples():
    """ρ 随采样拓宽（最差维的候选档覆盖）上升。"""
    from discovery.coverage import grid_coverage
    space = [("w", [40, 60, 80]), ("t", [1, 3, 5])]
    few = [{"w": 40, "t": 1}, {"w": 60, "t": 3}]               # 每维 2/3
    many = [{"w": 40, "t": 1}, {"w": 60, "t": 3}, {"w": 80, "t": 5}, {"w": 40, "t": 3}]  # 每维 3/3
    assert grid_coverage(many, space) > grid_coverage(few, space)
    assert grid_coverage(few, space) == 2 / 3
    assert grid_coverage(many, space) == 1.0


def test_grid_coverage_dedup():
    """同维重复采同一档不算新覆盖（边际口径下重复值自然去重）。"""
    from discovery.coverage import grid_coverage
    space = [("w", [40, 60, 80])]
    dups = [{"w": 40}, {"w": 40}, {"w": 40}]
    assert grid_coverage(dups, space) == 1 / 3


def test_grid_coverage_is_worst_dim():
    """min 聚合：一维全覆盖救不了另一维的盲区（防伪收敛的否决面）。"""
    from discovery.coverage import grid_coverage
    space = [("w", [40, 60, 80]), ("t", [1, 3, 5])]
    # w 三档全覆盖，t 只采过 1 → ρ 被 t 维钳制 = 1/3
    samples = [{"w": 40, "t": 1}, {"w": 60, "t": 1}, {"w": 80, "t": 1}]
    assert grid_coverage(samples, space) == 1 / 3


def test_grid_coverage_empty_samples():
    """空采样返 0.0（不许在零信息上过闸）。"""
    from discovery.coverage import grid_coverage
    space = [("w", [40, 60, 80])]
    assert grid_coverage([], space) == 0.0


def test_grid_coverage_default_uses_param_space():
    """不传 param_space 时用 sampler.PARAM_SPACE（21 维）。"""
    from discovery.coverage import grid_coverage
    from discovery.sampler import PARAM_SPACE, sample_search
    batch = sample_search(n_sobol=5, n_random=5, seed=1)
    rho = grid_coverage(batch)   # 默认 PARAM_SPACE
    assert 0.0 < rho < 1.0


def test_coverage_gate():
    """判据④：ρ≥阈值→达标（允许其他判据自停）；ρ<阈值→否决。"""
    from discovery.coverage import coverage_gate
    assert coverage_gate(0.9, threshold=0.8) is True
    assert coverage_gate(0.8, threshold=0.8) is True   # 含等
    assert coverage_gate(0.5, threshold=0.8) is False
