# -*- coding: utf-8 -*-
"""最终审查跟进修复的覆盖测试：
1. core/indicator.atr() 须保留 rolling warm-up 期 NaN（不静默替换成 1e-9 伪 ATR）。
   （ATR 已从 factors/micro_momentum 迁到 core/indicator——Phase 1·Task 3 因子体系剥离。）

注：原第 2 项「backtest/engine.run_minute 移动止损」测试已在 Phase 1·Task 4 随通用
回测引擎整体删除。蔡森上线前验证由 Phase 2 专用回放验证器承担，不再需要通用回测引擎。
"""
import numpy as np
import pandas as pd


def test_atr_preserves_warmup_nan_not_fake_value():
    """atr() warm-up 期（前 window-1 根）须是 NaN，绝不能被 .where 静默替换成 1e-9 伪 ATR。"""
    from core.indicator import atr
    idx = pd.date_range("2024-01-02 09:30", periods=20, freq="min")
    df = pd.DataFrame({"high": [11.0] * 20, "low": [9.0] * 20, "close": [10.0] * 20}, index=idx)
    a = atr(df, window=14)
    # 前 13 根（window-1）须为 NaN（warm-up），不是 1e-9
    assert a.iloc[:13].isna().all(), "warm-up 期应保留 NaN，不应被伪造成 1e-9"
    # 第 14 根起为正值（非 NaN）
    assert a.iloc[13] == 2.0
