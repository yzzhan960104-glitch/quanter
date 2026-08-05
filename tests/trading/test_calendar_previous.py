# -*- coding: utf-8 -*-
"""B1：calendar.previous_trading_day 单元测试。

物理意图（SSoT Phase B 断点-2 · 基准日零漂移）：
    expired_positions 改 pre_open 现算，基准日 = 上一交易日 = previous_trading_day(today)。
    与 next_trading_day 对称：从 date_str-1 起逐日 is_trading_day 回溯，最多 15 自然日。

注入式日历测试（不硬编码真实日历）：
    monkeypatch calendar.is_trading_day 用固定交易日集合，避免真实 trade_cal 缓存随时间
    变化导致断言假绿/假红（例如硬编码 "2026-08-04" 在真实日历中可能恰为非交易日，
    或测试运行时 fetch_trade_cal 拉取不同年份结果漂移）。
"""
from trading import calendar


def test_previous_trading_day_injected_calendar(monkeypatch):
    """previous_trading_day：注入 is_trading_day 桩，避免真实日历缓存变化致假绿/假红。"""
    # 桩：2026-08-04/08-01/07-31 是交易日，08-03/08-02 非交易日（模拟周末）
    trading = {"2026-08-04", "2026-08-01", "2026-07-31"}
    monkeypatch.setattr(calendar, "is_trading_day", lambda d: d in trading)
    assert calendar.previous_trading_day("2026-08-04") == "2026-08-01"  # 跳过 08-03/02
    assert calendar.previous_trading_day("2026-08-01") == "2026-07-31"


def test_previous_trading_day_consecutive_trading_days(monkeypatch):
    """连续交易日：前一自然日即交易日，直接返回（最常见路径）。"""
    trading = {"2026-06-02", "2026-06-01"}
    monkeypatch.setattr(calendar, "is_trading_day", lambda d: d in trading)
    assert calendar.previous_trading_day("2026-06-02") == "2026-06-01"


def test_previous_trading_day_fallback_extreme_holiday(monkeypatch):
    """极端长假（>15 自然日全非交易日）→ 兜底返回 date-1（defensive，A 股无此场景）。

    物理意图：保留兜底防 IndexError；该返回值可能非交易日，交由上层 pre_open 的
    is_trading_day 守卫自然告警拦截（宁可显式失败不静默错算超期）。
    """
    monkeypatch.setattr(calendar, "is_trading_day", lambda d: False)  # 全非交易日
    assert calendar.previous_trading_day("2026-02-20") == "2026-02-19"
