"""微观动量爆发 + ATR 波动率 + Risk Parity 头寸的数值正确性。

- breakout_signal：均线密集后突破，方向锁定（上行→末期非负）。
- atr：恒正、防除零 Inf。
- risk_parity_weight：ATR 小→头寸大（反比）。
"""
import numpy as np
import pandas as pd
from factors.micro_momentum import breakout_signal, atr, risk_parity_weight


def test_breakout_signal_direction():
    idx = pd.date_range("2024-01-02", periods=60, freq="min")
    close = pd.Series(np.linspace(10, 12, 60), index=idx)  # 单边上行
    df = pd.DataFrame({"close": close, "high": close + 0.1, "low": close - 0.1}, index=idx)
    sig = breakout_signal(df)
    assert sig.iloc[-1] in (1, 0) and sig.iloc[-5:].sum() >= 0  # 上行→末期非负


def test_atr_positive_and_risk_parity_inverse():
    idx = pd.date_range("2024-01-02", periods=40, freq="min")
    df = pd.DataFrame({"high": np.linspace(11, 12, 40),
                       "low": np.linspace(9, 10, 40),
                       "close": np.linspace(10, 11, 40)}, index=idx)
    a = atr(df, window=14)
    assert (a.dropna() > 0).all()
    w1 = risk_parity_weight(0.5, budget=1e6)
    w2 = risk_parity_weight(2.0, budget=1e6)
    assert w1 > w2   # ATR 小→头寸大（反比）


def test_atr_no_inf():
    idx = pd.date_range("2024-01-02", periods=40, freq="min")
    df = pd.DataFrame({"high": [11] * 40, "low": [9] * 40, "close": [10] * 40}, index=idx)  # ATR 常数
    a = atr(df, window=14)
    assert not np.isinf(a.dropna()).any()
