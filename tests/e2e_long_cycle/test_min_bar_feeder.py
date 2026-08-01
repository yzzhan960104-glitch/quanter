# -*- coding: utf-8 -*-
"""V3：MinBarFeeder stk_mins 时点切片累积 + 日线降级。"""
from __future__ import annotations

import asyncio
import pandas as pd
from datetime import date, time
from unittest.mock import patch


def _fake_stk_mins_df():
    """造 5 根 5min bar（9:30-09:55），high 递增测累积。"""
    return pd.DataFrame([
        {"ts_code": "300001.SZ", "trade_time": "2026-07-01 09:30:00", "close": 10.0, "high": 10.2, "low": 9.8},
        {"ts_code": "300001.SZ", "trade_time": "2026-07-01 09:35:00", "close": 10.3, "high": 10.5, "low": 10.0},
        {"ts_code": "300001.SZ", "trade_time": "2026-07-01 09:40:00", "close": 10.1, "high": 10.4, "low": 9.9},
        {"ts_code": "300001.SZ", "trade_time": "2026-07-01 09:45:00", "close": 10.6, "high": 10.8, "low": 10.1},
        {"ts_code": "300001.SZ", "trade_time": "2026-07-01 09:50:00", "close": 10.4, "high": 10.7, "low": 10.3},
    ])


def test_feed_cumulative_high_low_up_to_timepoint():
    """feed(sym, T 日, up_to=09:40) → 累积 high=max(9:30-09:40) low=min close=09:40 末根。"""
    from tests.e2e_long_cycle.min_bar_feeder import MinBarFeeder

    feeder = MinBarFeeder(stk_mins_loader=lambda sym, d: _fake_stk_mins_df())
    quotes = feeder.feed(["300001.SZ"], date(2026, 7, 1), up_to=time(9, 40))

    assert "300001.SZ" in quotes
    q = quotes["300001.SZ"]
    # 9:30-9:40 三根：high max=10.5（9:35），low min=9.8（9:30），close=10.1（9:40 末根）
    assert q["high"] == 10.5
    assert q["low"] == 9.8
    assert q["last_price"] == 10.1


def test_feed_caches_per_sym_per_day():
    """同标的同日多次 feed 只调一次 stk_mins_loader（tmp cache，防限频）。"""
    from tests.e2e_long_cycle.min_bar_feeder import MinBarFeeder

    call_count = {"n": 0}
    def loader(sym, d):
        call_count["n"] += 1
        return _fake_stk_mins_df()

    feeder = MinBarFeeder(stk_mins_loader=loader)
    feeder.feed(["300001.SZ"], date(2026, 7, 1), up_to=time(10, 0))
    feeder.feed(["300001.SZ"], date(2026, 7, 1), up_to=time(11, 0))  # 同标的同日
    assert call_count["n"] == 1  # cache 命中


def test_feed_degrades_to_daily_when_stk_mins_empty():
    """stk_mins 返空（停牌/限频）→ 降级 data_lake 日线 high/low + 告警标记。"""
    from tests.e2e_long_cycle.min_bar_feeder import MinBarFeeder

    feeder = MinBarFeeder(
        stk_mins_loader=lambda sym, d: pd.DataFrame(),  # 空（停牌）
        daily_loader=lambda sym, d: {"high": 11.0, "low": 9.5, "close": 10.0},  # 日线降级
    )
    quotes = feeder.feed(["300001.SZ"], date(2026, 7, 1), up_to=time(10, 0))
    q = quotes["300001.SZ"]
    assert q["high"] == 11.0 and q["low"] == 9.5  # 日线降级值
    assert feeder.degraded  # 降级标记（供 ReportBuilder §5）


async def _patch_get_quotes_and_await(feeder):
    """协程体内 await get_quotes（对齐生产 await 形态 engine.py:1036）。"""
    from trading import qmt_market_data
    with feeder.patch_get_quotes():
        return await qmt_market_data.get_quotes(["300001.SZ"])


def test_patch_get_quotes_injects_feed_result():
    """patch_get_quotes() → monkeypatch qmt_market_data.get_quotes 返 feed 结果。

    对齐生产 await 形态：engine.py:1036 ``quotes = await qmt_market_data.get_quotes(syms)``。
    Why asyncio.run：pytest-asyncio mode=strict，项目惯例（V2 test_signal_scanner.py:38）
    用 asyncio.run 包裹协程，避免 async def test_ + marker 的样板。
    """
    from tests.e2e_long_cycle.min_bar_feeder import MinBarFeeder

    feeder = MinBarFeeder(stk_mins_loader=lambda sym, d: _fake_stk_mins_df())
    feeder.set_context(symbols=["300001.SZ"], t_date=date(2026, 7, 1), up_to=time(9, 40))
    quotes = asyncio.run(_patch_get_quotes_and_await(feeder))  # 与生产一致：await
    assert quotes["300001.SZ"]["high"] == 10.5  # 经 monkeypatch 注入
