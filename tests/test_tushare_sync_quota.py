# -*- coding: utf-8 -*-
"""_fetch_with_guard 按 quota_type 路由到对应限频桶（Plan Task 2）。

物理意图：双桶限频（基础500/特色300）的选桶逻辑——_fetch_with_guard 接受 quota_type
参数，basic 走 tushare_rate_limiter_basic，special 走 tushare_rate_limiter_special。
缺省 basic（向后兼容现有未显式传 quota_type 的调用）。

测试隔离（2026-08-11 修正）：patch **模块属性**（data.tushare_sync.tushare_rate_limiter_*）
而非对象方法（patch.object 全局单例的 acquire）。原写法在全量跑时会失败——先执行的
他测可能替换 data.tushare_sync 模块属性指向新对象，patch.object(原单例, "acquire")
对新对象无效 → _fetch_with_guard 拿到的 limiter 不含被 patch 的 acquire → assert
"called 0 times"。改为 patch 模块属性：_fetch_with_guard 经模块属性查找拿到测试注入
的 MagicMock，完全自隔离，不受全局单例对象身份漂移影响（单测/全量跑均绿）。
"""
from unittest.mock import patch, MagicMock
import pandas as pd

import data.tushare_sync as tsync


def _make_pro(fake_df):
    """造一个 mock pro，其 some_api 方法返 fake_df。"""
    pro = MagicMock()
    pro.some_api = MagicMock(return_value=fake_df)
    return pro


def test_quota_basic_走基础桶():
    """quota_type=basic 应 acquire 基础桶令牌，不动特色桶。"""
    df = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20250101"], "close": [10.0]})
    pro = _make_pro(df)
    fake_basic = MagicMock()
    fake_special = MagicMock()
    with patch("data.tushare_sync.get_pro", return_value=pro), \
         patch("data.tushare_sync.tushare_rate_limiter_basic", fake_basic), \
         patch("data.tushare_sync.tushare_rate_limiter_special", fake_special), \
         patch("data.tushare_sync.tushare_breaker") as fake_breaker:
        # allow_request=True 跳过熔断 OPEN 冷却循环，聚焦限频桶路由断言
        fake_breaker.allow_request.return_value = True
        tsync._fetch_with_guard("some_api", quota_type="basic", trade_date="20250101")
        fake_basic.acquire.assert_called_once_with(1.0)
        fake_special.acquire.assert_not_called()


def test_quota_special_走特色桶():
    """quota_type=special 应 acquire 特色桶令牌，不动基础桶。"""
    df = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20250101"], "close": [10.0]})
    pro = _make_pro(df)
    fake_basic = MagicMock()
    fake_special = MagicMock()
    with patch("data.tushare_sync.get_pro", return_value=pro), \
         patch("data.tushare_sync.tushare_rate_limiter_basic", fake_basic), \
         patch("data.tushare_sync.tushare_rate_limiter_special", fake_special), \
         patch("data.tushare_sync.tushare_breaker") as fake_breaker:
        fake_breaker.allow_request.return_value = True
        tsync._fetch_with_guard("some_api", quota_type="special", trade_date="20250101")
        fake_special.acquire.assert_called_once_with(1.0)
        fake_basic.acquire.assert_not_called()


def test_quota_缺省走基础桶():
    """quota_type 缺省（不传）走基础桶（向后兼容现有 _fetch_with_guard 调用）。"""
    df = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20250101"]})
    pro = _make_pro(df)
    fake_basic = MagicMock()
    with patch("data.tushare_sync.get_pro", return_value=pro), \
         patch("data.tushare_sync.tushare_rate_limiter_basic", fake_basic), \
         patch("data.tushare_sync.tushare_breaker") as fake_breaker:
        fake_breaker.allow_request.return_value = True
        tsync._fetch_with_guard("some_api", trade_date="20250101")
        fake_basic.acquire.assert_called_once_with(1.0)
