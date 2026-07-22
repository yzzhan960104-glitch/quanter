# -*- coding: utf-8 -*-
"""信号转下单单测（Task 3）。

Layer2 阶段1：signals 改为 list[Signal]（frozen dataclass），测试构造 Signal 替代 dict。
"""
from strategies.signal import Signal
from trading.signal_runner import build_orders_from_signals, PlannedOrder


def test_build_orders_position_sizing():
    """单标的：capital 100万 × pos_cap 0.05 = 5万，entry 10 元 → 5000 股 → 整手 5000。
    附 stop_price（颈线-stop_mult×atr）+ take_profit（颈线+tp_mult×H）。"""
    signals = [Signal(
        symbol="600000.SH", entry_price=10.0, neckline=9.5, bottom=8.5,
        signal_type="neckline",
    )]
    orders = build_orders_from_signals(
        signals, capital=1_000_000.0, pos_cap=0.05,
        atr_map={"600000.SH": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0},
    )
    assert len(orders) == 1
    o = orders[0]
    assert o.order.symbol == "600000.SH"
    assert o.order.side == "buy"
    assert o.order.qty == 5000                      # 5万/10元=5000，整100手
    assert o.order.price == 10.0
    # 止损 = 颈线9.5 - 2×0.5 = 8.5；止盈 = 颈线9.5 + 2×(9.5-8.5)=11.5
    assert abs(o.stop_price - 8.5) < 1e-9
    assert abs(o.take_profit - 11.5) < 1e-9


def test_build_orders_skip_missing_atr():
    """无 ATR 的标的跳过（防 None 运算）。"""
    signals = [Signal(symbol="X.SH", entry_price=10.0, neckline=9.5, bottom=8.5)]
    orders = build_orders_from_signals(
        signals, capital=1_000_000.0, pos_cap=0.05,
        atr_map={}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert orders == []
