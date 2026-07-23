# tests/trading/test_calendar_expected.py
"""期望最新交易日计算（数据实时性检查的基石）。"""
from datetime import datetime, time
from unittest.mock import patch
from trading.calendar import expected_latest_trade_day


def test_after_close_on_trading_day_returns_today():
    """盘后(>=15:00)且今天是交易日 → 期望今天（T 日数据应已落湖）。"""
    now = datetime(2026, 7, 23, 18, 30)  # 周四 18:30
    with patch("trading.calendar.is_trading_day", return_value=True):
        assert expected_latest_trade_day(now) == "2026-07-23"


def test_before_close_returns_previous_trade_day():
    """盘中或盘前 → 期望上一个交易日（T-1 数据应齐全）。"""
    now = datetime(2026, 7, 23, 10, 0)  # 周四盘中
    with patch("trading.calendar.is_trading_day", side_effect=lambda d: d == "2026-07-22"):
        assert expected_latest_trade_day(now) == "2026-07-22"


def test_weekend_rolls_back_to_friday():
    """周末 → 期望上周五（回溯找上一个交易日）。"""
    now = datetime(2026, 7, 25, 12, 0)  # 周六
    with patch("trading.calendar.is_trading_day", side_effect=lambda d: d == "2026-07-24"):
        assert expected_latest_trade_day(now) == "2026-07-24"


def test_non_trading_day_after_close_rolls_back():
    """节假日盘后 → 回溯到节前最后一个交易日。"""
    now = datetime(2026, 10, 2, 18, 0)  # 国庆假
    with patch("trading.calendar.is_trading_day", return_value=False):
        # 全部非交易日 → 兜底返 today（极端，长假中无交易日）
        assert expected_latest_trade_day(now) == "2026-10-02"
