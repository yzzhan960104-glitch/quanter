# -*- coding: utf-8 -*-
"""Deflated Sharpe Ratio 测试（闭式，纯 stdlib）。"""


def test_dsr_decreases_with_more_trials():
    """多重比较修正：试越多，同 sharpe 的 DSR 越低（最高更可能是运气）。"""
    from discovery.dsr import deflated_sharpe
    s_few = deflated_sharpe(sharpe=2.0, n_trials=10, n_obs=100)
    s_many = deflated_sharpe(sharpe=2.0, n_trials=1000, n_obs=100)
    assert s_few > s_many


def test_dsr_increases_with_sharpe():
    """更高 sharpe → DSR 更高（更显著）。"""
    from discovery.dsr import deflated_sharpe
    low = deflated_sharpe(sharpe=1.0, n_trials=10, n_obs=100)
    high = deflated_sharpe(sharpe=3.0, n_trials=10, n_obs=100)
    assert high > low


def test_dsr_increases_with_observations():
    """更长样本 → DSR 更高（样本量越大越显著）。"""
    from discovery.dsr import deflated_sharpe
    short = deflated_sharpe(sharpe=2.0, n_trials=10, n_obs=30)
    long = deflated_sharpe(sharpe=2.0, n_trials=10, n_obs=500)
    assert long > short


def test_dsr_range():
    """DSR ∈ [0,1]（是概率）。"""
    from discovery.dsr import deflated_sharpe
    for sh in [-1.0, 0.0, 1.0, 5.0]:
        v = deflated_sharpe(sharpe=sh, n_trials=5, n_obs=100)
        assert 0.0 <= v <= 1.0


def test_dsr_single_trial_no_multiple_comparison():
    """n_trials=1 → 无多重比较，SR_max=0，DSR 仅看 sharpe 显著性。"""
    from discovery.dsr import deflated_sharpe
    v = deflated_sharpe(sharpe=2.0, n_trials=1, n_obs=100)
    assert 0.0 <= v <= 1.0
