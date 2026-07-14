# -*- coding: utf-8 -*-
"""通用 Tushare 湖同步器测试：配置驱动 + 分页 + 断点续传 + 落湖。

设计意图（反黑盒测试）：
- fake_pro 替身 mock 掉 get_pro / tushare_rate_limiter / tushare_breaker，使测试
  **完全不依赖真 Tushare token / 网络环境**（开发机可能未配 TNSKHDATA_TOKEN），
  仅验证同步器的分页/断点/落湖逻辑正确性。
- 通过 TUSHARE_DATASETS[key] 临时覆盖落湖路径到 tmp_path，保证测试隔离无副作用。
"""
import os
import pandas as pd
import pytest


class _FakePro:
    """tushare pro 替身：按 api_name 返回可控 DataFrame。

    Why __getattr__：pro 接口方法（pro.income / pro.stock_basic ...）在运行时由
    tushare DataApi 动态分发，测试替身用 __getattr__ 一次性兜底所有 api_name，
    避免逐方法硬编码；同时记录调用序列供断言「shard 已存在即跳过」等行为。
    """
    def __init__(self):
        self.calls = []  # 记录 (api_name, kwargs)
        self._data = {
            "income": pd.DataFrame({
                "ts_code": ["000001.SZ"] * 3,
                "ann_date": ["20240101", "20240401", "20240701"],
                "end_date": ["20231231", "20240331", "20240630"],
                "total_revenue": [1e9, 1.1e9, 1.2e9],
                "n_income": [1e8, 1.1e8, 1.2e8],
            }),
        }

    def __getattr__(self, api_name):
        def _call(**kwargs):
            self.calls.append((api_name, kwargs))
            return self._data.get(api_name, pd.DataFrame())
        return _call


@pytest.fixture
def fake_pro(monkeypatch):
    """mock pro 接口 + 限频/熔断器（acquire 直通、breaker 永远放行）。

    Why 同时 mock 三个：sync_dataset 经 _fetch_with_guard 串联 rate_limiter → breaker
    → get_pro，三道闸门任一未被 mock 都会触达真实 tushare/网络。fixture 一次性
    把数据路径短路，让测试聚焦分页/落湖逻辑本身。
    """
    fake = _FakePro()
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: fake)
    monkeypatch.setattr("data.tushare_sync.tushare_rate_limiter",
                        type("L", (), {"acquire": lambda self, n: None})())
    monkeypatch.setattr("data.tushare_sync.tushare_breaker",
                        type("B", (), {"allow_request": lambda self: True,
                                       "record_success": lambda self: None,
                                       "record_failure": lambda self: None})())
    return fake


def test_sync_dataset_by_symbol_multiindex(tmp_path, fake_pro, monkeypatch):
    """by=symbol 分页：逐标的拉取 → MultiIndex(date,symbol) 落湖。"""
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    # 注册一个测试数据集
    TUSHARE_DATASETS["fina_income"] = {
        "api": "income", "by": "symbol",  # 按标的分页
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_revenue,n_income",
        "lake": str(tmp_path / "income.parquet"),
    }
    LAKE_CONFIG["lakes"]["fina_income"] = TUSHARE_DATASETS["fina_income"]["lake"]
    from data.tushare_sync import sync_dataset
    sync_dataset("fina_income", "2024-01-01", "2024-12-31",
                 symbols=["000001.SZ"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["fina_income"]["lake"])
    assert df.index.names == ["date", "symbol"]
    assert "total_revenue" in df.columns
    assert len(df) == 3


def test_sync_dataset_resume_skips_existing_shard(tmp_path, fake_pro, monkeypatch):
    """断点续传：shard 已存在即跳过（省配额）。"""
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    shard_dir = str(tmp_path / "shards")
    TUSHARE_DATASETS["fina_income"] = {
        "api": "income", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_revenue",
        "lake": str(tmp_path / "income.parquet"),
        "shard_dir": shard_dir,
    }
    os.makedirs(shard_dir)
    # 预置 shard（模拟已拉过 000001.SZ）
    pd.DataFrame({"total_revenue": [1e9]},
                 index=pd.DatetimeIndex(["2024-01-01"], name="ann_date")
                 ).to_parquet(os.path.join(shard_dir, "000001.SZ.parquet"))
    from data.tushare_sync import sync_dataset
    sync_dataset("fina_income", "2024-01-01", "2024-12-31",
                 symbols=["000001.SZ"], resume=True)
    # fake_pro 未被调（shard 已存在跳过）
    assert fake_pro.calls == []
