# -*- coding: utf-8 -*-
"""交易日历单测（Task 1 + Phase 1.5 任务5 token 读取统一）。"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
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


def test_fetch_trade_cal_uses_tushare_compat_get_pro(tmp_path):
    """fetch_trade_cal 走 data._tushare_compat.get_pro（Phase 1.5 任务5 统一凭证入口）。

    Why 此测试存在：原 calendar 自己读 os.getenv(TUSHARE_TOKEN)/TNSKHDATA_TOKEN 与
    _tushare_compat 口径分叉，直连 tushare 切换后会读错 token。固化「calendar 走
    get_pro」契约防止回归到自读 env 的旧分叉。
    """
    cache = tmp_path / "trade_cal_2026.json"
    # 构造 pro.trade_cal 返两交易日（is_open=1）+ 一非交易日
    pro = MagicMock()
    pro.trade_cal = MagicMock(return_value=pd.DataFrame({
        "cal_date": ["20260101", "20260102", "20260103"],
        "is_open": [1, 1, 0],
    }))
    with patch.object(calendar, "_cache_path", return_value=cache), \
         patch("data._tushare_compat.get_pro", return_value=pro) as mock_get_pro:
        days = calendar.fetch_trade_cal(2026)
    # 仅 is_open=1 的 01-01 / 01-02 被取（格式化为 YYYY-MM-DD）
    assert days == ["2026-01-01", "2026-01-02"]
    assert mock_get_pro.call_count == 1  # 走 get_pro 而非自读 env
    # 缓存写入（下次命中不拉 tushare）
    assert cache.exists()


def test_fetch_trade_cal_weekday_fallback_when_no_token(tmp_path):
    """无 token / get_pro 抛异常 → weekday 兜底（不识节假日仅识周末）。"""
    cache = tmp_path / "trade_cal_2026.json"
    with patch.object(calendar, "_cache_path", return_value=cache), \
         patch("data._tushare_compat.get_pro", side_effect=RuntimeError("no token")):
        days = calendar.fetch_trade_cal(2026)
    # weekday 兜底：2026-01-03 是周六（不返），2026-01-05 周一（返）
    assert "2026-01-05" in days
    assert "2026-01-03" not in days  # 周六被 weekday 过滤
    assert not cache.exists()  # 兜底不写缓存（避免脏缓存污染下次）
