# -*- coding: utf-8 -*-
"""最终审查跟进修复的覆盖测试：
1. core/indicator.atr() 须保留 rolling warm-up 期 NaN（不静默替换成 1e-9 伪 ATR）。
   （ATR 已从 factors/micro_momentum 迁到 core/indicator——Phase 1·Task 3 因子体系剥离。）
2. backtest/engine.run_minute 移动止损须主动触发（价格跌破既有止损线 → reason='移动止损' 平仓）。
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


def test_run_minute_trailing_stop_triggers():
    """移动止损：持仓期间价格回落跌破既有止损线 → 触发 reason='移动止损' 平仓。

    构造：Day1 平台建仓（T+1 冻结）→ Day2 先涨（抬升 running_high/trailing_stop）
    后跌（跌破 trailing_stop）。tp/sl 阈值设远以隔离，确保只有移动止损触发。
    """
    from backtest.engine import BacktestEngine
    d1 = pd.date_range("2024-01-02 09:30", periods=20, freq="min")
    d2 = pd.date_range("2024-01-03 09:30", periods=30, freq="min")
    idx = d1.append(d2)
    rise = np.linspace(10, 15, 10)
    crash = np.linspace(15, 8, 20)
    close = np.concatenate([np.full(20, 10.0), rise, crash])
    df = pd.DataFrame({
        "open": close, "high": close + 0.5, "low": close - 0.5,
        "close": close, "volume": np.full(50, 1000.0),
    }, index=idx)
    signal = pd.Series(0.8, index=idx)
    events = []
    BacktestEngine(initial_capital=1_000_000).run_minute(
        df, signal, "000001.SZ", atr_window=14, sl_pct=0.5, tp_pct=2.0,
        trail_k=2.0, event_emitter=lambda e: events.append(e),
    )
    risk = [e for e in events if e.get("type") == "risk"]
    # ★ 移动止损须触发（tp=30/sl=5 都不先触发，只有 trailing ~13.5 先被跌穿）
    assert any("移动止损" in e.get("reason", "") for e in risk), \
        f"未触发移动止损，risk 事件: {risk[-5:]}"
