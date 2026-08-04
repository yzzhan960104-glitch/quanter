# -*- coding: utf-8 -*-
"""_sync_by_symbol 的 adj_api 前复权增强测试（Plan Task 3）。

物理意图：cfg['adj_api'] 存在时，_sync_by_symbol 额外拉 adj_factor，按
price_qfq = raw × adj / latest（latest=区间最新）重建 OHLCV 价格列，与既有
a_shares_daily.parquet（由 sync_data_lake.fetch_qfq 生产）字节级一致。volume/amount 不复权。
"""
from unittest.mock import MagicMock
import os
import pandas as pd
import pytest

import data.tushare_sync as tsync


def test_adj_api_触发前复权重建(tmp_path, monkeypatch):
    """cfg['adj_api'] 存在时，raw daily × adj_factor/latest → 价格列前复权。"""
    ts_code = "000001.SZ"
    # 原始价（除权前）：两日 close=10/11；adj_factor 首日=1.0、次日=2.0（次日除权翻倍）
    # latest_adj=2.0（区间最新），前复权后：首日 close=10×1/2=5.0，次日 close=11×2/2=11.0（基准日不变）
    raw = pd.DataFrame({
        "ts_code": [ts_code] * 2,
        "trade_date": ["20250101", "20250102"],
        "open": [10.0, 11.0], "high": [10.5, 11.5],
        "low": [9.5, 10.5], "close": [10.0, 11.0],
        "vol": [1000.0, 1100.0], "amount": [10000.0, 11000.0],
    })
    adj = pd.DataFrame({
        "ts_code": [ts_code] * 2,
        "trade_date": ["20250101", "20250102"],
        "adj_factor": [1.0, 2.0],
    })
    pro = MagicMock()
    pro.daily = MagicMock(return_value=raw)
    pro.adj_factor = MagicMock(return_value=adj)
    monkeypatch.setattr(tsync, "get_pro", lambda: pro)
    # 跳过限频/熔断真实阻塞
    monkeypatch.setattr(tsync.tushare_rate_limiter_basic, "acquire", lambda tokens=1.0, timeout=None: None)
    monkeypatch.setattr(tsync.tushare_breaker, "allow_request", lambda: True)
    monkeypatch.setattr(tsync.tushare_breaker, "record_success", lambda: None)

    lake_out = str(tmp_path / "out.parquet")
    cfg = {
        "api": "daily", "by": "symbol", "adj_api": "adj_factor",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "rename": {"vol": "volume"},
        "lake": lake_out,
        "quota_type": "basic",
    }
    monkeypatch.setitem(tsync.TUSHARE_DATASETS, "_test_daily", cfg)
    shard_dir = str(tmp_path / "shards")
    monkeypatch.setattr(tsync, "_shard_dir", lambda k: shard_dir)

    tsync.sync_dataset("_test_daily", "2025-01-01", "2025-01-02",
                       symbols=[ts_code], resume=False)

    out = pd.read_parquet(lake_out)
    # 首日 close 前复权 = 10 × 1.0 / 2.0 = 5.0
    row0 = out.xs(pd.Timestamp("2025-01-01"), level="date").loc[ts_code]
    assert row0["close"] == pytest.approx(5.0, rel=1e-6)
    # 次日（基准日）close = 11 × 2.0 / 2.0 = 11.0（不变）
    row1 = out.xs(pd.Timestamp("2025-01-02"), level="date").loc[ts_code]
    assert row1["close"] == pytest.approx(11.0, rel=1e-6)
    # volume 不复权
    assert row0["volume"] == pytest.approx(1000.0, rel=1e-6)


def test_adj_api_缺省时不触发前复权(tmp_path, monkeypatch):
    """cfg 无 adj_api 时，_sync_by_symbol 走原逻辑（不拉 adj_factor，价格不复权）。"""
    ts_code = "000001.SZ"
    raw = pd.DataFrame({
        "ts_code": [ts_code], "trade_date": ["20250101"],
        "open": [10.0], "high": [10.5], "low": [9.5], "close": [10.0],
        "vol": [1000.0], "amount": [10000.0],
    })
    pro = MagicMock()
    pro.daily = MagicMock(return_value=raw)
    pro.adj_factor = MagicMock(return_value=pd.DataFrame())  # 不应被调用
    monkeypatch.setattr(tsync, "get_pro", lambda: pro)
    monkeypatch.setattr(tsync.tushare_rate_limiter_basic, "acquire", lambda tokens=1.0, timeout=None: None)
    monkeypatch.setattr(tsync.tushare_breaker, "allow_request", lambda: True)
    monkeypatch.setattr(tsync.tushare_breaker, "record_success", lambda: None)

    lake_out = str(tmp_path / "out.parquet")
    cfg = {
        "api": "daily", "by": "symbol",  # 无 adj_api
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "rename": {"vol": "volume"},
        "lake": lake_out,
        "quota_type": "basic",
    }
    monkeypatch.setitem(tsync.TUSHARE_DATASETS, "_test_daily_raw", cfg)
    monkeypatch.setattr(tsync, "_shard_dir", lambda k: str(tmp_path / "shards_raw"))

    tsync.sync_dataset("_test_daily_raw", "2025-01-01", "2025-01-02",
                       symbols=[ts_code], resume=False)
    # adj_factor 不应被调用（无 adj_api）
    pro.adj_factor.assert_not_called()
    out = pd.read_parquet(lake_out)
    row = out.xs(pd.Timestamp("2025-01-01"), level="date").loc[ts_code]
    assert row["close"] == pytest.approx(10.0, rel=1e-6)  # 原始价未复权


# ─── Task 9：qfq 除权标的历史全量重算 ─────────────────────────────────────────
def test_recompute_symbol_rebuilds_full_history_baseline(monkeypatch):
    """除权标的：历史行用新窗口最新 adj 重建（旧行不再停留在旧基线）。

    物理意图：adj_factor 在窗口内从 0.9→1.0 变化（除权事件），latest_adj=1.0（最新日）。
    前复权公式 close_qfq = raw × adj / latest：
      12-29: 10.0 × 0.9 / 1.0 = 9.0（历史行基准回退至新基线，消除除权断崖）
      01-03: 11.2 × 1.0 / 1.0 = 11.2（基准日不变）
    _recompute_symbol 走 _fetch_with_guard（tushare_sync.basic 桶 500/min），测试 monkeypatch
    绕过真实限频/熔断，直接调 FakePro。
    """
    from data.tools import sync_daily_incremental as sdl

    class FakePro:
        # _fetch_with_guard 内部走 getattr(pro, api_name)(**kwargs)，FakePro 按 kwargs 接收
        def daily(self, ts_code, start_date, end_date, **_):
            return pd.DataFrame({
                "ts_code": [ts_code] * 3,
                "trade_date": ["20231229", "20240102", "20240103"],
                "open": [9.8, 10.5, 11.0],
                "high": [10.2, 10.8, 11.3],
                "low": [9.7, 10.2, 10.8],
                "close": [10.0, 10.6, 11.2],
                "vol": [100, 110, 120],
                "amount": [1000, 1100, 1200],
            })

        def adj_factor(self, ts_code, start_date, end_date, **_):
            return pd.DataFrame({
                "ts_code": [ts_code] * 3,
                "trade_date": ["20231229", "20240102", "20240103"],
                "adj_factor": [0.9, 0.95, 1.0],
            })

    # 绕过 _fetch_with_guard 的 get_pro/限频/熔断：直接调 FakePro 的方法
    fake_pro = FakePro()
    monkeypatch.setattr(sdl, "_fetch_with_guard",
                        lambda api_name, *, quota_type="basic", **kw: getattr(fake_pro, api_name)(**kw))

    out = sdl._recompute_symbol(fake_pro, "000001.SZ", "20240103")
    assert not out.empty, "_recompute_symbol 应返回非空 DataFrame"
    closes = out.sort_index()["close"].astype(float)
    assert closes.iloc[-1] == pytest.approx(11.2)   # 最新日 adj/latest = 1.0
    assert closes.iloc[0] == pytest.approx(9.0)     # 12-29: 10.0 × 0.9 / 1.0


def test_recompute_symbol_空数据返空DF(monkeypatch):
    """raw 拉空（停牌/退市/接口异常）→ 返空 DF，不抛异常（守数据底座鲁棒性）。"""
    from data.tools import sync_daily_incremental as sdl

    class EmptyPro:
        def daily(self, **_):
            return pd.DataFrame()

        def adj_factor(self, **_):
            return pd.DataFrame()

    empty_pro = EmptyPro()
    monkeypatch.setattr(sdl, "_fetch_with_guard",
                        lambda api_name, *, quota_type="basic", **kw: getattr(empty_pro, api_name)(**kw))
    out = sdl._recompute_symbol(empty_pro, "000001.SZ", "20240103")
    assert out.empty
