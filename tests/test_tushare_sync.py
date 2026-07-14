# -*- coding: utf-8 -*-
"""通用 Tushare 湖同步器测试：配置驱动 + 分页 + 断点续传 + 落湖。

设计意图（反黑盒测试）：
- fake_pro 替身 mock 掉 get_pro / tushare_rate_limiter / tushare_breaker，使测试
  **完全不依赖真 Tushare token / 网络环境**（开发机可能未配 TNSKHDATA_TOKEN），
  仅验证同步器的分页/断点/落湖逻辑正确性。
- 通过 TUSHARE_DATASETS[key] 临时覆盖落湖路径到 tmp_path，保证测试隔离无副作用。
"""
import copy
import os
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _isolate_tushare_registry():
    """深拷贝 TUSHARE_DATASETS + LAKE_CONFIG['lakes']，测试后还原原对象。

    Why autouse 深拷贝：两个测试就地覆盖全局 TUSHARE_DATASETS['fina_income'] 和
    LAKE_CONFIG['lakes']['fina_income']，若不还原会污染后续测试（测试顺序依赖、
    隔离性破坏）。深拷贝保证测试内 mutate 不影响原注册表，yield 后还原引用即可
    让其他模块看到原始未被改动的配置。copy.deepcopy 处理嵌套 dict（lakes 子键）。
    """
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    saved_datasets = copy.deepcopy(TUSHARE_DATASETS)
    saved_lakes = copy.deepcopy(LAKE_CONFIG["lakes"])
    yield
    TUSHARE_DATASETS.clear()
    TUSHARE_DATASETS.update(saved_datasets)
    LAKE_CONFIG["lakes"].clear()
    LAKE_CONFIG["lakes"].update(saved_lakes)


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


def test_build_multiindex_by_date_symbol_from_column(tmp_path):
    """by=date 模式：symbol 必须来自 shard 内 ts_code 列，而非交易日文件名。

    Why 此测试：by=date shard 是「单日全市场」（文件名=交易日如 20240105.parquet，
    shard 内含多标的的 ts_code 列）。早期实现一律从文件名取 symbol，导致每行被标
    symbol='20240105'，symbol 级全错。本测试直接构造单日全市场 shard，验证合并后
    MultiIndex 的 symbol 来自真实标的码（000001.SZ / 600000.SH），不是交易日串。
    """
    from data.tushare_sync import _build_multiindex
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    # 构造单日全市场 shard：DatetimeIndex(ann_date) + ts_code 列 + 数据列
    # 两个真实标的，同一天 ann_date=2024-01-05
    shard_df = pd.DataFrame({
        "ts_code": ["000001.SZ", "600000.SH"],
        "end_date": ["20231231", "20231231"],
        "total_revenue": [1e9, 2e9],
    }, index=pd.DatetimeIndex(["2024-01-05", "2024-01-05"], name="ann_date"))
    # 文件名是交易日（by=date 模式的 shard 命名约定）
    shard_df.to_parquet(shard_dir / "20240105.parquet")

    out = str(tmp_path / "moneyflow.parquet")
    _build_multiindex(str(shard_dir), date_col="ann_date",
                      symbol_col="ts_code", out=out, by="date")

    df = pd.read_parquet(out)
    # MultiIndex 名
    assert df.index.names == ["date", "symbol"]
    # symbol 必须是真实标的码，不是交易日 '20240105'（防退化核心断言）
    symbols = set(df.index.get_level_values("symbol"))
    assert symbols == {"000001.SZ", "600000.SH"}, (
        f"by=date symbol 应来自 ts_code 列，实际：{symbols}")
    # 行数 + 数据列保持
    assert len(df) == 2
    assert "total_revenue" in df.columns
