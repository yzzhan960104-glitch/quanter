"""FactorAnalyzer：IC 方向性、分层单调性、空输入安全。"""
import numpy as np
import pandas as pd
from factors.analyzer import FactorAnalyzer


def _make_perfect_factor():
    # 因子值与远期收益完全单调正相关 → IC 应显著为正
    idx = pd.date_range("2024-01-01", periods=30)
    rng = np.random.default_rng(0)
    factor = pd.DataFrame(rng.uniform(0, 1, size=(30, 5)),
                          index=idx, columns=list("ABCDE"))
    # 远期收益 = 因子 + 小噪声
    fwd = factor + rng.normal(0, 0.05, size=factor.shape)
    return factor, fwd


def test_ic_positive_for_monotone_relation():
    factor, fwd = _make_perfect_factor()
    out = FactorAnalyzer().compute_ic(factor, fwd)
    assert out["ic_mean"] > 0.5
    assert "ic_ir" in out and "t_stat" in out


def test_fractile_monotone_top_above_bottom():
    factor, fwd = _make_perfect_factor()
    out = FactorAnalyzer().fractile_analysis(factor, fwd, n_groups=5)
    ls = out["long_short"].dropna()
    # 多空价差均值应为正（top 组收益 > bottom 组）
    assert ls.mean() > 0


def test_compute_ic_empty_safe():
    factor = pd.DataFrame(np.nan, index=[0], columns=["A"])
    fwd = pd.DataFrame(np.nan, index=[0], columns=["A"])
    out = FactorAnalyzer().compute_ic(factor, fwd)
    # 不抛异常即可
    assert "ic_mean" in out
