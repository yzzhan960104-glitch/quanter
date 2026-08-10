# -*- coding: utf-8 -*-
"""数据快照扩容：新增数据集 schema 完整性 + quota_type 归类测试（Plan Task 5/6/7）。

物理意图：钉死新注册数据集的必填字段（api/by/date_col/symbol_col/fields/lake/quota_type）
与 quota_type 归类正确性（基础桶 basic / 特色桶 special），防配置漂移。
"""
from config.registry import TUSHARE_DATASETS, DATASET_REGISTRY


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


# ============ Task 6：特色桶新数据集 ============
def test_特色桶新数据集已注册():
    for k in ("cyq_chips", "daily_basic", "stk_factor_pro"):
        cfg = TUSHARE_DATASETS[k]
        assert cfg["quota_type"] == "special", f"{k} 应归特色桶(300/min)"
        _assert_schema(k)


def test_moneyflow_归特色桶():
    """资金流向按 Tushare 官方分类属特色数据，归 300/min 特色桶。"""
    assert TUSHARE_DATASETS["moneyflow"]["quota_type"] == "special"


def test_cyq_chips_逐标的分页():
    """cyq_chips 接口要求 ts_code（Task 11 dry-run 订正：by=date→by=symbol）。"""
    cfg = TUSHARE_DATASETS["cyq_chips"]
    assert cfg["by"] == "symbol", "cyq_chips 接口要求 ts_code，应 by=symbol"
    assert cfg["universe"] == "stock"
    assert "price" in cfg["fields"] and "percent" in cfg["fields"]


def test_stk_factor_pro_fields带bfq后缀():
    """stk_factor_pro 列名带 _bfq/_hfq/_qfq 后缀（Task 11 dry-run 订正幻觉列）。"""
    cfg = TUSHARE_DATASETS["stk_factor_pro"]
    # 订正后用 _bfq（不复权）版本，非裸 macd/rsi_6/cci
    assert "macd_bfq" in cfg["fields"] and "macd" not in cfg["fields"].split(","), \
        "stk_factor_pro 用 macd_bfq（非裸 macd，原是幻觉列）"
    assert "cci_bfq" in cfg["fields"] and "rsi_bfq_6" in cfg["fields"]


# ============ Task 7：OHLCV 前复权三频 ============
def test_OHLCV三频_by_symbol_adj_api():
    """weekly/monthly 走 by=symbol + adj_api 前复权（Task 3 增强）。

    T13-A：daily 已从 TUSHARE_DATASETS 退役（改走增量 sync_daily_incremental），此处只
    守卫仍走通用同步器的周/月线。
    """
    for k, api in [("weekly", "weekly"), ("monthly", "monthly")]:
        cfg = TUSHARE_DATASETS[k]
        assert cfg["api"] == api
        assert cfg["by"] == "symbol"
        assert cfg["adj_api"] == "adj_factor", f"{k} 必须配 adj_api 触发前复权"
        assert cfg["quota_type"] == "basic"
        assert "close" in cfg["fields"] and "vol" in cfg["fields"]
        assert cfg.get("rename", {}).get("vol") == "volume", f"{k} 必须 vol→volume 归一"


def test_daily_复用既有a_shares_daily湖():
    """周/月线复用既有 a_shares_* 湖；daily 已退役（T13-A 双轨收口）。

    T13-A：daily 从 TUSHARE_DATASETS 删除（改走增量），此处只守卫 weekly/monthly 的湖路径。
    """
    assert "daily" not in TUSHARE_DATASETS, "daily 应已退役（T13-A 双轨收口）"
    assert TUSHARE_DATASETS["weekly"]["lake"] == "data_lake/a_shares_weekly.parquet"
    assert TUSHARE_DATASETS["monthly"]["lake"] == "data_lake/a_shares_monthly.parquet"


# ============ Task 8：DATASET_REGISTRY 元信息（前端 DataLakeView 反射） ============
def test_新数据集_元信息完整():
    """所有新 TUSHARE_DATASETS key 必须在 DATASET_REGISTRY 有元信息（前端反射）。"""
    new_keys = ["stock_basic", "hs_const_sh", "hs_const_sz",
                "cyq_chips", "daily_basic", "stk_factor_pro",
                "weekly", "monthly"]
    for k in new_keys:
        assert k in DATASET_REGISTRY, f"{k} 缺 DATASET_REGISTRY 元信息"
        meta = DATASET_REGISTRY[k]
        assert meta["source"] == "Tushare"
        assert meta["script"] == "data/tools/sync_tushare.py", f"{k} script 应统一 sync_tushare.py"
        assert "market" in meta and "granularity" in meta and "freshness_hours" in meta


def test_daily_脚本切增量入口():
    """daily 的 script 切到 sync_daily_incremental.py（T13-A 双轨收口，唯一写入口 = 增量）。

    原 sync_tushare.py 全量重建路径已封死（T12 实证致 1020万→3200 抹除）。
    """
    assert DATASET_REGISTRY["daily"]["script"] == "data/tools/sync_daily_incremental.py"
