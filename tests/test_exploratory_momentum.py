"""探索性动量：横截面排名、波动率调整、赫斯顿指数数值正确性。"""
import numpy as np
import pandas as pd
from factors.exploratory_momentum import (
    cross_sectional_momentum, vol_adjusted_momentum, hurst_exponent)


def test_cross_sectional_momentum_ranks():
    # 两只标的，构造 20+ 行
    idx = pd.date_range("2024-01-01", periods=25)
    returns = pd.DataFrame({"A": np.linspace(0.01, 0.02, 25),
                            "B": np.linspace(-0.02, -0.01, 25)}, index=idx)
    mom = cross_sectional_momentum(returns, window=20)
    # A 滚动收益 > B → A 排名百分位应 > B
    last = mom.iloc[-1]
    assert last["A"] > last["B"]


def test_vol_adjusted_momentum_no_div_by_zero():
    idx = pd.date_range("2024-01-01", periods=25)
    rng = np.random.default_rng(0)
    close = pd.DataFrame({"A": 100 + np.cumsum(rng.normal(size=25))}, index=idx)
    returns = close.pct_change()
    high = close + 1; low = close - 1
    m = vol_adjusted_momentum(returns, high, low, close, window=10, atr_window=10)
    assert m.shape == close.shape
    assert not np.isinf(m.dropna()).any().any()  # 无 Inf（防除零）


def test_hurst_persistent_series_above_half():
    # 强自相关随机游走累积序列，赫斯顿应 > 0.5（持续性）
    rng = np.random.default_rng(42)
    s = pd.Series(np.cumsum(rng.normal(0.01, 0.1, size=500)))
    h = hurst_exponent(s, max_k=50)
    # 范围合法性 + 持续性方向锁定（此前仅断 0<h<1，名实不符：哪怕均值回复 h≈0.1 也通过）
    assert 0.0 < h < 1.0
    # 锁定持续性：累积序列自相关强，实现实测 H≈0.986，应明显 >0.5
    assert h > 0.5
