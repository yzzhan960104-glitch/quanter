# -*- coding: utf-8 -*-
"""数据快照扩容：新增数据集 schema 完整性 + quota_type 归类测试（Plan Task 5/6/7）。

物理意图：钉死新注册数据集的必填字段（api/by/date_col/symbol_col/fields/lake/quota_type）
与 quota_type 归类正确性（基础桶 basic / 特色桶 special），防配置漂移。
"""
from config.registry import TUSHARE_DATASETS


REQUIRED_FIELDS = {"api", "by", "date_col", "symbol_col", "fields", "lake", "quota_type"}


def _assert_schema(k):
    cfg = TUSHARE_DATASETS[k]
    assert REQUIRED_FIELDS.issubset(cfg.keys()), f"{k} 缺必填字段：缺 {REQUIRED_FIELDS - set(cfg.keys())}"


# ============ Task 5：基础桶新数据集 ============
def test_stock_basic_已注册且基础桶():
    cfg = TUSHARE_DATASETS["stock_basic"]
    assert cfg["api"] == "stock_basic"
    assert cfg["by"] == "single"
    assert cfg["quota_type"] == "basic"
    assert "ts_code" in cfg["fields"]
    _assert_schema("stock_basic")


def test_hs_const_沪深两湖已注册():
    for k in ("hs_const_sh", "hs_const_sz"):
        cfg = TUSHARE_DATASETS[k]
        assert cfg["api"] == "hs_const"
        assert cfg["by"] == "single"
        assert cfg["quota_type"] == "basic"
        _assert_schema(k)
    assert TUSHARE_DATASETS["hs_const_sh"]["params"]["hs_type"] == "SH"
    assert TUSHARE_DATASETS["hs_const_sz"]["params"]["hs_type"] == "SZ"


def test_concept_detail_按概念id分页():
    cfg = TUSHARE_DATASETS["concept_detail"]
    assert cfg["api"] == "concept_detail"
    assert cfg["by"] == "symbol"
    assert cfg["universe"] == "concept"
    assert cfg["code_param"] == "id"
    assert cfg["quota_type"] == "basic"
    _assert_schema("concept_detail")


# ============ Task 6：特色桶新数据集 ============
def test_特色桶新数据集已注册():
    for k in ("cyq_chips", "daily_basic", "stk_factor_pro"):
        cfg = TUSHARE_DATASETS[k]
        assert cfg["quota_type"] == "special", f"{k} 应归特色桶(300/min)"
        _assert_schema(k)


def test_moneyflow_归特色桶():
    """资金流向按 Tushare 官方分类属特色数据，归 300/min 特色桶。"""
    assert TUSHARE_DATASETS["moneyflow"]["quota_type"] == "special"


def test_cyq_chips_按日分页():
    """cyq_chips 逐价位分布，by=date 单日全市场一次返（数据量大，按日分片）。"""
    cfg = TUSHARE_DATASETS["cyq_chips"]
    assert cfg["by"] == "date"
    assert "price" in cfg["fields"] and "percent" in cfg["fields"]


# ============ Task 7：OHLCV 前复权三频 ============
def test_OHLCV三频_by_symbol_adj_api():
    """daily/weekly/monthly 走 by=symbol + adj_api 前复权（Task 3 增强）。"""
    for k, api in [("daily", "daily"), ("weekly", "weekly"), ("monthly", "monthly")]:
        cfg = TUSHARE_DATASETS[k]
        assert cfg["api"] == api
        assert cfg["by"] == "symbol"
        assert cfg["adj_api"] == "adj_factor", f"{k} 必须配 adj_api 触发前复权"
        assert cfg["quota_type"] == "basic"
        assert "close" in cfg["fields"] and "vol" in cfg["fields"]
        assert cfg.get("rename", {}).get("vol") == "volume", f"{k} 必须 vol→volume 归一"


def test_daily_复用既有a_shares_daily湖():
    """daily 复用既有 a_shares_daily.parquet（903万行），保持一致性。"""
    assert TUSHARE_DATASETS["daily"]["lake"] == "data_lake/a_shares_daily.parquet"
    assert TUSHARE_DATASETS["weekly"]["lake"] == "data_lake/a_shares_weekly.parquet"
    assert TUSHARE_DATASETS["monthly"]["lake"] == "data_lake/a_shares_monthly.parquet"
