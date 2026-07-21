# -*- coding: utf-8 -*-
"""交易日历单测（Task 1）。"""
from datetime import datetime
from trading import calendar


def test_is_trading_day_uses_cache(monkeypatch, tmp_path):
    """缓存命中不调 Tushare；周末返 False。"""
    cache = tmp_path / "trade_cal_2026.json"
    cache.write_text('["2026-07-21", "2026-07-22"]', encoding="utf-8")
    monkeypatch.setattr(calendar, "_cache_path", lambda y: cache if y == 2026 else tmp_path / f"trade_cal_{y}.json")
    assert calendar.is_trading_day("2026-07-21") is True   # 周二在缓存
    assert calendar.is_trading_day("2026-07-19") is False  # 周日不在缓存


def test_is_intraday_session():
    """A 股盘中时段判定（9:30-11:30 / 13:00-15:00）。"""
    assert calendar.is_intraday_session(datetime(2026, 7, 21, 10, 0)) is True
    assert calendar.is_intraday_session(datetime(2026, 7, 21, 12, 0)) is False  # 午休
    assert calendar.is_intraday_session(datetime(2026, 7, 21, 14, 30)) is True
    assert calendar.is_intraday_session(datetime(2026, 7, 21, 15, 30)) is False  # 收盘后
