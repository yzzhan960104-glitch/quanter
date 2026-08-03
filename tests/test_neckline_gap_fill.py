# -*- coding: utf-8 -*-
"""P0-1 跳空/跌停成交保守修正单测（2026-08-03 Phase A）。

物理意图：原 simulate_exit 的止损按 stop 价"完美成交"，未建模跳空低开（open<stop
时真实市价单按更差的开盘价附近成交）。本测试锁定：
    - 止损触发日 open<stop → 按 min(stop, open)=open 成交（保守，防过拟合"紧止损"）；
    - 非跳空场景（open>stop 盘中破位）→ 仍按 stop 成交（原语义不变）；
    - 返回 dict 新增 stop_gap / same_day_both 字段（供策略级统计与报告归因）。
"""
import pandas as pd

from strategies.neckline.backtest import EXEC_DEFAULTS, simulate_exit
from strategies.neckline.method_v0 import DEFAULTS


def _df(opens, highs, lows, closes):
    """构造 10 根日 K 的测试 DF（信号日在 idx0）。"""
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1000] * len(opens),
    }, index=pd.date_range("2025-01-01", periods=len(opens)))


def test_stop_loss_fills_at_open_when_gap_down():
    """止损触发日跳空低开（open<stop）→ 按 open 成交（比 stop 更差，保守）。"""
    # 颈线=10，底=8，ATR=1 → buy_limit=10.5（0.5ATR），stop=9.0（1ATR）。
    # idx1 回踩成交（low 9.5 ≤ 10.5，open 10 → entry=10）；
    # idx2 跳空低开 open=8.5 < stop=9.0 且 low=8.0 ≤ stop → 止损按 8.5 成交。
    df = _df(
        opens=[10.2, 10.0, 8.5, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0],
        highs=[10.3, 10.5, 8.8, 9.2, 9.2, 9.2, 9.2, 9.2, 9.2, 9.2],
        lows=[10.0, 9.5, 8.0, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9],
        closes=[10.2, 10.2, 8.6, 9.1, 9.1, 9.1, 9.1, 9.1, 9.1, 9.1],
    )
    sim = simulate_exit(df, 0, c_star=10.0, bottom=8.0, atr_val=1.0,
                        exec={**EXEC_DEFAULTS, "buy_limit_atr_mult": 0.5},
                        id_cfg={**DEFAULTS, "stop_atr_mult": 1.0})
    assert sim is not None
    assert sim["exit_reason"] == "stop_loss"
    assert sim["entry"] == 10.0
    assert sim["stop_gap"] is True
    # open=8.5 成交 → 毛损 -15%，低于（更差于）按 stop=9.0 的 -10%
    assert sim["lot1_pnl_pct"] < -10.0


def test_stop_loss_fills_at_stop_without_gap():
    """非跳空场景（open>stop 盘中破位）→ 仍按 stop 成交（原语义不变）。"""
    df = _df(
        opens=[10.2, 10.0, 9.5, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0],
        highs=[10.3, 10.5, 9.6, 9.2, 9.2, 9.2, 9.2, 9.2, 9.2, 9.2],
        lows=[10.0, 9.5, 8.8, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9],
        closes=[10.2, 10.2, 9.3, 9.1, 9.1, 9.1, 9.1, 9.1, 9.1, 9.1],
    )
    sim = simulate_exit(df, 0, c_star=10.0, bottom=8.0, atr_val=1.0,
                        exec={**EXEC_DEFAULTS, "buy_limit_atr_mult": 0.5},
                        id_cfg={**DEFAULTS, "stop_atr_mult": 1.0})
    assert sim is not None
    assert sim["exit_reason"] == "stop_loss"
    assert sim["stop_gap"] is False
    # open=9.5 > stop=9.0 → 按 stop 成交，毛损 ≈ -10%
    assert -10.5 < sim["lot1_pnl_pct"] < -9.5


def test_same_day_cancel_vs_fill_marked():
    """等待期同日 high≥cancel_on 且 low≤buy_limit → skip_target_met + same_day_both=True。"""
    df = _df(
        opens=[10.2, 10.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0],
        highs=[10.3, 15.0, 9.2, 9.2, 9.2, 9.2, 9.2, 9.2, 9.2, 9.2],
        lows=[10.0, 9.0, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9],
        closes=[10.2, 10.2, 9.1, 9.1, 9.1, 9.1, 9.1, 9.1, 9.1, 9.1],
    )
    # cancel_on = 颈线 + 2×H = 10 + 2×2 = 14；idx1 high=15 ≥ 14 且 low=9 ≤ buy_limit=10.5
    sim = simulate_exit(df, 0, c_star=10.0, bottom=8.0, atr_val=1.0,
                        exec={**EXEC_DEFAULTS, "buy_limit_atr_mult": 0.5,
                              "cancel_thresh_mult": 2.0},
                        id_cfg=DEFAULTS)
    assert sim is not None
    assert sim["exit_reason"] == "skip_target_met"
    assert sim["same_day_both"] is True


def test_cancel_without_fill_not_marked():
    """等待期 high≥cancel_on 但 low>buy_limit（纯冲高无回踩）→ same_day_both=False。"""
    df = _df(
        opens=[10.2, 10.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0],
        highs=[10.3, 15.0, 9.2, 9.2, 9.2, 9.2, 9.2, 9.2, 9.2, 9.2],
        lows=[10.0, 11.0, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9],
        closes=[10.2, 10.2, 9.1, 9.1, 9.1, 9.1, 9.1, 9.1, 9.1, 9.1],
    )
    sim = simulate_exit(df, 0, c_star=10.0, bottom=8.0, atr_val=1.0,
                        exec={**EXEC_DEFAULTS, "buy_limit_atr_mult": 0.5,
                              "cancel_thresh_mult": 2.0},
                        id_cfg=DEFAULTS)
    assert sim is not None
    assert sim["exit_reason"] == "skip_target_met"
    assert sim["same_day_both"] is False
