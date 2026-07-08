# -*- coding: utf-8 -*-
"""RiskManager 测试：宏观系数三态、HV 过滤、流动性、仓位 5% 钳制。"""
import numpy as np
import pandas as pd
import pytest
from caisen.config import StrategyConfig
from caisen.risk import RiskManager


def test_macro_coef_three_states(monkeypatch):
    """regime +1→1.0, 0→0.6, -1→0.0。"""
    rm = RiskManager(StrategyConfig())
    class FakeRegime:
        def compute(self, d): return d  # 透传：用日期值模拟 regime
    rm.regime = FakeRegime()
    assert rm.macro_position_coef(1) == pytest.approx(1.0)
    assert rm.macro_position_coef(0) == pytest.approx(0.6)
    assert rm.macro_position_coef(-1) == pytest.approx(0.0)

def test_liquidity_filter():
    """近30日均成交额 ≥ 1亿 通过。"""
    rm = RiskManager(StrategyConfig())
    idx = pd.RangeIndex(40)
    df = pd.DataFrame({"amount": [2e8]*40}, index=idx)
    assert rm.liquidity_filter(df.tail(30)) is True
    df_low = pd.DataFrame({"amount": [5e7]*40}, index=idx)
    assert rm.liquidity_filter(df_low.tail(30)) is False

def test_position_size_capped_at_5pct():
    """仓位被 max_position_pct 5% 硬钳。"""
    rm = RiskManager(StrategyConfig())
    shares = rm.position_size(aum=1_000_000, entry=10.0, stop=9.0, coef=1.0)
    # 5% 上限 = 50000 元 / entry 10 → 5000 股，向下取整到 100 整手
    assert shares <= 5000
    assert shares % 100 == 0
