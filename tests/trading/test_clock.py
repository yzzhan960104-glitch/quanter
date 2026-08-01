# -*- coding: utf-8 -*-
"""C-6 V1：trading/clock.py 单一时间源单测。

物理意图（spec §3.1 · [[eod-date-offbyone-fix]] 教训）：
    now/today/trading_day 三函数是 trading 包时间统一口子。today=pre_open 读口径，
    trading_day=eod 落盘口径（next_trading_day(today)），命名区分读/写避免 eod/pre_open 混淆。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import trading.clock as clock
from trading.calendar import next_trading_day


def test_now_returns_datetime():
    """now() 返 datetime（事件时间戳用）。"""
    result = clock.now()
    assert isinstance(result, datetime)


def test_today_format():
    """today() 返 YYYY-MM-DD 字符串（pre_open 读 plan key 口径）。"""
    fixed = datetime(2026, 7, 28, 15, 30, 0)
    with patch("trading.clock.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        assert clock.today() == "2026-07-28"


def test_trading_day_equals_next_trading_day_of_today():
    """trading_day() == next_trading_day(today())（eod 落盘 key 口径）。

    物理意图：eod（T 日盘后）落 plan_T+1，pre_open（T+1 开盘前）读 plan_T+1。
    trading_day 命名区分读/写口径——避免 eod/pre_open key 错位。
    """
    fixed = datetime(2026, 7, 28, 15, 30, 0)  # 周二
    with patch("trading.clock.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        # next_trading_day("2026-07-28") 真实算（依赖 calendar，周二→周三 2026-07-29）
        assert clock.trading_day() == next_trading_day("2026-07-28")


def test_trading_day_neq_today():
    """trading_day() != today()（key 错位防线——eod 落盘日 ≠ 今日）。"""
    fixed = datetime(2026, 7, 28, 15, 30, 0)
    with patch("trading.clock.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        assert clock.trading_day() != clock.today()
