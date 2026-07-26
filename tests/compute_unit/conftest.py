# -*- coding: utf-8 -*-
"""compute_unit 测试共享 fixture。合成数据,不依赖真实 data_lake(快)。

与 tests/discovery/conftest.py 同口径(synth_sym_df 复制同款),保证 runner 等价红线测试
(C6:compute_unit 结果 == discovery evaluate 直跑)用同一份合成 universe,口径可信。
"""
import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def synth_sym_df():
    """合成单标的 OHLCV(250 根日线 2025 全年),与 tests/discovery/conftest.py 同款。

    价格平稳上行 + 噪声,让颈线法有信号又不退化。列对齐 scan_symbol 期望。
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
        "amount": rng.integers(1e7, 1e8, 250),   # 千元单位,≥1e5=1亿
    }, index=idx)


@pytest.fixture
def fixed_params():
    """固定 21 维 params(确定性,不读 logs/param_iter_state.json)。

    值对齐 discovery 冠军 fallback(tests/discovery/conftest.py:champion_params fallback)。
    runner 等价测试用此,保证跨实现口径一致。
    """
    return {
        "window": 80, "min_touches": 2, "min_suppression": 0.5,
        "local_extrema_window": 3, "min_bottoms": 3, "breakout_vol_mult": 1.0,
        "min_rr": 2.0, "max_h_atr": 4.0, "stop_atr_mult": 1.5, "tp_h_mult": 1.5,
        "decay_tau": 30,
        "max_holding": 15, "max_wait": 8, "cooldown": 5, "buy_limit_atr_mult": 1.5,
        "tp1_h_mult": 1.5, "tp1_portion": 0.3, "cancel_thresh_mult": 2.0,
        "trailing_grace": 0, "trailing_step": 0.15, "trailing_floor": 0.0,
    }


@pytest.fixture
def synth_universe(synth_sym_df):
    """合成 universe(1 只标的),供 _eval_batch / _eval_one 注入。"""
    return {"300001.SZ": synth_sym_df}
