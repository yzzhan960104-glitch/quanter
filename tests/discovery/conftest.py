# -*- coding: utf-8 -*-
"""discovery 测试共享 fixture。合成数据，不依赖真实 data_lake（快）。"""
import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def synth_sym_df():
    """合成单标的 OHLCV（250 根日线，2025 全年），供 scan_symbol 跑通。

    价格平稳上行 + 噪声，让颈线法有信号又不至于退化。列对齐 scan_symbol 期望。
    """
    idx = pd.bdate_range("2025-01-01", periods=250)
    rng = np.random.default_rng(42)
    close = 10.0 + np.cumsum(rng.normal(0.02, 0.3, 250))
    high = close * (1 + rng.uniform(0, 0.03, 250))
    low = close * (1 - rng.uniform(0, 0.03, 250))
    opn = close + rng.normal(0, 0.1, 250)
    return pd.DataFrame({
        "open": opn, "high": high, "low": low, "close": close,
        "volume": rng.integers(1e6, 1e7, 250),
        "amount": rng.integers(1e7, 1e8, 250),  # 千元单位，≥1e5=1亿
    }, index=idx)


@pytest.fixture
def champion_params():
    """当前 param_iter 冠军参数（state.json best，21 维）。供 oos 集成测试用。"""
    return {
        "window": 80, "min_touches": 2, "min_suppression": 0.5,
        "local_extrema_window": 5, "min_bottoms": 2, "breakout_vol_mult": 1.0,
        "min_rr": 1.0, "max_h_atr": 5.0, "stop_atr_mult": 1.0, "tp_h_mult": 2.5,
        "decay_tau": 60,
        "max_holding": 20, "max_wait": 3, "cooldown": 8, "buy_limit_atr_mult": 1.0,
        "tp1_h_mult": 0.5, "tp1_portion": 0.3, "cancel_thresh_mult": None,
        "trailing_grace": 0, "trailing_step": 0.05, "trailing_floor": 0.0,
    }
