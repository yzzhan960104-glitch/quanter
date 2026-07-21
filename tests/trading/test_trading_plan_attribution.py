# -*- coding: utf-8 -*-
"""eod_plan order_dict 透传归因 + trading_plan save/load 往返保真。

物理意图：Task 5 让 PlannedOrder 携带 experiment_id/experiment_weight，
本套测试验证 trading_plan 落盘 JSON 透传这两个归因字段——既保证新 plan
往返保真（report 阶段聚合实验归因的物理基础），又保证老 plan（无归因字段）
向后兼容不崩（report 归「未归因」桶）。
"""
import pytest

from trading import trading_plan


def test_save_plan_preserves_experiment_attribution(tmp_path, monkeypatch):
    """orders 嵌套 dict 带 experiment_id/experiment_weight，save→load 往返保真。

    Why：Task8 report 要按 experiment_id 聚合归因，落盘 JSON 必须原样带回
    归因字段，否则 report 阶段拿不到实验分组信息。
    """
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    orders = [{
        "order": {"symbol": "000001.SZ", "qty": 1000, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
        "experiment_id": "neckline_v6_20260722", "experiment_weight": 0.2,
    }]
    trading_plan.save_plan("2026-07-22", orders)
    loaded = trading_plan.load_plan("2026-07-22")
    assert loaded["orders"][0]["experiment_id"] == "neckline_v6_20260722"
    assert loaded["orders"][0]["experiment_weight"] == 0.2


def test_old_plan_without_attribution_loads_ok(tmp_path, monkeypatch):
    """老 plan（无归因字段）load 不崩（向后兼容，report 归「未归因」桶）。

    Why：实验系统上线前已有大量历史 plan 文件不带归因字段，load 时不能因
    KeyError 崩掉——Task8 report 应将这类订单归入「未归因」桶单独统计。
    """
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    orders = [{"order": {"symbol": "X", "qty": 100, "side": "buy", "price": 10},
               "stop_price": 9, "take_profit": 11}]
    trading_plan.save_plan("2026-07-20", orders)
    loaded = trading_plan.load_plan("2026-07-20")
    assert "experiment_id" not in loaded["orders"][0]   # 老无字段，不崩
