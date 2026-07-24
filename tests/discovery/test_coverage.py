# -*- coding: utf-8 -*-
"""覆盖度度量④测试（纯函数，网格占用率）。"""


def test_grid_coverage_grows_with_samples():
    """ρ 随不重复采样组合数增加而上升。"""
    from discovery.coverage import grid_coverage
    space = [("w", [40, 60, 80]), ("t", [1, 3, 5])]   # 9 单元
    few = [{"w": 40, "t": 1}, {"w": 60, "t": 3}]               # 2 unique
    many = [{"w": 40, "t": 1}, {"w": 60, "t": 3}, {"w": 80, "t": 5}, {"w": 40, "t": 3}]  # 4 unique
    assert grid_coverage(many, space) > grid_coverage(few, space)
    assert grid_coverage(few, space) == 2 / 9
    assert grid_coverage(many, space) == 4 / 9


def test_grid_coverage_dedup():
    """重复组合不算（去重）。"""
    from discovery.coverage import grid_coverage
    space = [("w", [40, 60, 80])]   # 3 单元
    dups = [{"w": 40}, {"w": 40}, {"w": 40}]
    assert grid_coverage(dups, space) == 1 / 3


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
