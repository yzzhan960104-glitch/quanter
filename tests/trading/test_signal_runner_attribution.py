# -*- coding: utf-8 -*-
"""signal_runner 归因 + 权重：PlannedOrder 带 experiment_id，budget 按 weight 分配。

背景（Task 5）：
- 实验系统配置中心（experiment/ Task 1-4）已上线，现在把实验归因透传到 T-1 信号→订单转换层。
- Task 7 的 `_eod` 会在生成 signal dict 时注入 `experiment_id` / `experiment_weight`。
- 本模块验证：归因字段落到 PlannedOrder；资金额度按各自权重分配；老 signal（无归因）向后兼容。
"""
import pytest

from trading.signal_runner import PlannedOrder, build_orders_from_signals


def _signal(symbol="000001.SZ", entry=10.0, neckline=10.5, bottom=9.5,
            exp_id="e1", weight=0.2):
    """构造带实验归因的颈线法信号 dict。"""
    return {"symbol": symbol, "entry_price": entry, "neckline": neckline,
            "bottom": bottom, "experiment_id": exp_id, "experiment_weight": weight}


def test_planned_order_carries_experiment_id():
    """PlannedOrder 含 experiment_id + experiment_weight 归因字段。"""
    orders = build_orders_from_signals(
        [_signal(exp_id="neckline_v6", weight=0.3)],
        capital=1_000_000, pos_cap=0.05,
        atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert len(orders) == 1
    assert orders[0].experiment_id == "neckline_v6"
    assert orders[0].experiment_weight == 0.3


def test_budget_scaled_by_weight():
    """qty = weight × capital × pos_cap / entry，向下取整 100 股。"""
    full = build_orders_from_signals([_signal(weight=1.0)], capital=1_000_000,
        pos_cap=0.05, atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    half = build_orders_from_signals([_signal(weight=0.5)], capital=1_000_000,
        pos_cap=0.05, atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert full[0].order.qty > half[0].order.qty
    assert full[0].order.qty % 100 == 0   # A 股 100 整手


def test_signal_without_attribution_defaults_weight_one():
    """老 signal（无 experiment_weight）默认 weight=1.0，experiment_id=""（向后兼容）。"""
    s = {"symbol": "000001.SZ", "entry_price": 10.0, "neckline": 10.5, "bottom": 9.5}
    orders = build_orders_from_signals([s], capital=1_000_000, pos_cap=0.05,
        atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert orders[0].experiment_weight == 1.0 and orders[0].experiment_id == ""
