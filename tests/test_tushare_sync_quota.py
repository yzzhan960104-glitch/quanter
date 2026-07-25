# -*- coding: utf-8 -*-
"""_fetch_with_guard 按 quota_type 路由到对应限频桶（Plan Task 2）。

物理意图：双桶限频（基础500/特色300）的选桶逻辑——_fetch_with_guard 接受 quota_type
参数，basic 走 tushare_rate_limiter_basic，special 走 tushare_rate_limiter_special。
缺省 basic（向后兼容现有未显式传 quota_type 的调用）。
"""
from unittest.mock import patch, MagicMock
import pandas as pd

import data.tushare_sync as tsync
from data.resilience import tushare_rate_limiter_basic, tushare_rate_limiter_special


def _make_pro(fake_df):
    """造一个 mock pro，其 some_api 方法返 fake_df。"""
    pro = MagicMock()
    pro.some_api = MagicMock(return_value=fake_df)
    return pro


def test_quota_basic_走基础桶():
    """quota_type=basic 应 acquire 基础桶令牌，不动特色桶。"""
    df = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20250101"], "close": [10.0]})
    pro = _make_pro(df)
    with patch("data.tushare_sync.get_pro", return_value=pro), \
         patch.object(tushare_rate_limiter_basic, "acquire") as acq_b, \
         patch.object(tushare_rate_limiter_special, "acquire") as acq_s:
        tsync._fetch_with_guard("some_api", quota_type="basic", trade_date="20250101")
        acq_b.assert_called_once()
        acq_s.assert_not_called()


def test_quota_special_走特色桶():
    """quota_type=special 应 acquire 特色桶令牌，不动基础桶。"""
    df = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20250101"], "close": [10.0]})
    pro = _make_pro(df)
    with patch("data.tushare_sync.get_pro", return_value=pro), \
         patch.object(tushare_rate_limiter_basic, "acquire") as acq_b, \
         patch.object(tushare_rate_limiter_special, "acquire") as acq_s:
        tsync._fetch_with_guard("some_api", quota_type="special", trade_date="20250101")
        acq_s.assert_called_once()
        acq_b.assert_not_called()


def test_quota_缺省走基础桶():
    """quota_type 缺省（不传）走基础桶（向后兼容现有 _fetch_with_guard 调用）。"""
    df = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20250101"]})
    pro = _make_pro(df)
    with patch("data.tushare_sync.get_pro", return_value=pro), \
         patch.object(tushare_rate_limiter_basic, "acquire") as acq_b:
        tsync._fetch_with_guard("some_api", trade_date="20250101")
        acq_b.assert_called_once()
