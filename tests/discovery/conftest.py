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
    """当前 param_iter 冠军参数（21 维）。供 oos 集成测试用。

    运行时从 logs/param_iter_state.json 读 state["best"]——硬编码会与 state 漂移
    （曾因硬编码 ≠ best 致 slow 测试 outer ann 失真：硬编码 13.4% vs 真 best ~145%+）。
    state 不存在时 fallback 到下方占位（值对齐最近一次 state best，仅作占位，
    正常开发环境 state.json 总存在）。
    """
    import json
    import os

    state_file = "logs/param_iter_state.json"
    if os.path.exists(state_file):
        with open(state_file, encoding="utf-8") as f:
            return json.load(f)["best"]
    # fallback（无 state 时占位；值对齐最近一次 state best）
    return {
        "window": 80, "min_touches": 2, "min_suppression": 0.5,
        "local_extrema_window": 3, "min_bottoms": 3, "breakout_vol_mult": 1.0,
        "min_rr": 2.0, "max_h_atr": 4.0, "stop_atr_mult": 1.5, "tp_h_mult": 1.5,
        "decay_tau": 30,
        "max_holding": 15, "max_wait": 8, "cooldown": 5, "buy_limit_atr_mult": 1.5,
        "tp1_h_mult": 1.5, "tp1_portion": 0.3, "cancel_thresh_mult": 2.0,
        "trailing_grace": 0, "trailing_step": 0.15, "trailing_floor": 0.0,
    }
