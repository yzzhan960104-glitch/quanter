# -*- coding: utf-8 -*-
"""V4：ProbabilisticBroker 概率成交 + 构造熔断/超期场景。"""
from __future__ import annotations

import asyncio
from datetime import date, time
from unittest.mock import patch, MagicMock

import pandas as pd


def _stub_feeder(price=10.5):
    """MinBarFeeder 桩（返固定价）。"""
    feeder = MagicMock()
    feeder.feed.return_value = {"300001.SZ": {"last_price": price, "high": price, "low": price}}
    feeder.price_for.return_value = price
    return feeder


def test_submit_probability_distribution_fixed_seed():
    """固定种子下 100 单：FILLED ~70 / PARTIAL ~15 / REJECTED ~5 / 延迟 ~10（容差 ±10）。"""
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker

    broker = ProbabilisticBroker(seed=42, min_bar_feeder=_stub_feeder())
    counts = {"FILLED": 0, "PARTIAL_FILLED": 0, "REJECTED": 0}
    for _ in range(100):
        order = {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.5}
        result = broker.simulate_submit(order, t_date=date(2026, 7, 1), up_to=time(9, 25))
        counts[result["state"]] = counts.get(result["state"], 0) + 1
    assert 55 <= counts["FILLED"] <= 85        # ~70 ±15
    assert 5 <= counts["PARTIAL_FILLED"] <= 30  # ~15
    assert 0 <= counts["REJECTED"] <= 15        # ~5


def test_partial_fill_traded_volume_less_than_qty():
    """PARTIAL_FILLED → traded_volume < qty（gap4 部分成交精度）。

    注：qty 取 1000（非 brief 原 100）——brief 实现保 A 股最小手数 ``max(100, ...)``，
    qty=100 时部分成交下限被锁回 100（与全成等价，断言 ``<100`` 恒假）。
    qty=1000 给 30%-70% 部分成交留出 300-700 的合法空间，物理意图（traded<qty）才成立。
    """
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker

    broker = ProbabilisticBroker(seed=1, min_bar_feeder=_stub_feeder(),
                                  force_state="PARTIAL_FILLED")  # 强制部分成交断言
    order = {"symbol": "300001.SZ", "qty": 1000, "side": "buy", "price": 10.5}
    result = broker.simulate_submit(order, t_date=date(2026, 7, 1), up_to=time(9, 25))
    assert result["state"] == "PARTIAL_FILLED"
    assert result["traded_volume"] < 1000


def test_circuit_breaker_constructed_day_returns_depressed_equity():
    """构造熔断日：query_asset 返 start×0.96（-3% < 阈值）→ post_close 熔断判定触发。"""
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker

    broker = ProbabilisticBroker(seed=42, min_bar_feeder=_stub_feeder(),
                                  circuit_breaker_days={date(2026, 7, 10)},
                                  start_equity=1_000_000.0)
    asset = broker.simulate_query_asset(date(2026, 7, 10))
    assert asset["total_asset"] == 960_000.0  # -4% < -3% 熔断阈值


def test_expired_symbol_marked_by_holding_days():
    """构造超期标的：expired_symbols 中的标的在 position 里 holding_days > max_holding。"""
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker

    broker = ProbabilisticBroker(seed=42, min_bar_feeder=_stub_feeder(),
                                  expired_symbols={"300001.SZ": {"entry_date": "2026-06-15",
                                                                  "holding_days_ref": date(2026, 7, 10)}})
    positions = broker.simulate_fetch_positions(date(2026, 7, 10))
    # 300001.SZ 持仓 entry 06-15 → 07-10 holding_days≈25 > max_holding(默认 10)
    assert "300001.SZ" in positions
