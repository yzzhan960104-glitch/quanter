# -*- coding: utf-8 -*-
"""宏观经济类 Tushare 数据集配置 + 落湖契约测试（Plan C Task 3-5）。

设计意图（DatetimeIndex 单时间序列 + 反格式假设）：
- **index_mode=datetime 契约**：宏观湖（CPI/PPI/GDP/PMI/Shibor）是单一时间序列，落
  DatetimeIndex（无 symbol 层），区别于股票湖的 MultiIndex(date, symbol)。_sync_single
  依据 cfg['index_mode']=='datetime' 分支重建时间索引；配置漏写会走原 single 路径落
  扁平 df（无时间索引），DataLakeReader 按日期切片直接 KeyError。
- **季/月/日频 format 推断**：_sync_single 按字符串形态分流（含 'Q' → PeriodIndex；
  6 位 → %Y%m；8 位 → %Y%m%d），format 错配会静默产出 NaT → dropna 清空整表。本测试
  守卫月频（cn_cpi month=YYYYMM）+ 季频（cn_gdp quarter=YYYYQ1）两条分支。
- **交易所日级统计**：szse_daily/sse_daily by=date（市场级时序，symbol_col=trade_date，
  symbol 层恒等于交易日），落 MultiIndex(date, symbol)，区别于宏观 single 的 DatetimeIndex。

fixture 复制说明：与 test_tushare_datasets_etf.py 同——完整复制 autouse
_isolate_tushare_registry + fake_pro fixture（conftest 未抽取，文件级作用域）。

注：szse_daily/sse_daily 的 LAKE_CONFIG key 与 TUSHARE_DATASETS key 一致（szse_daily/sse_daily），
仅 lake 路径用 mkt_daily_*.parquet（单一真相源：LAKE_CONFIG[key]==TUSHARE_DATASETS[key]['lake']）。
"""
import copy
import pandas as pd
import pytest

from config import TUSHARE_DATASETS, LAKE_CONFIG


@pytest.fixture(autouse=True)
def _isolate_tushare_registry():
    """深拷贝 TUSHARE_DATASETS + LAKE_CONFIG['lakes']，测试后还原原对象引用。

    Why autouse 深拷贝（与 stock/etf 文件同手法）：本文件的测试会就地覆盖全局
    TUSHARE_DATASETS[key]['lake']（重定向到 tmp_path）。若不还原，全局注册表会被污染。
    手法：clear()+update(saved) 保留原 dict 对象身份。
    """
    saved_datasets = copy.deepcopy(TUSHARE_DATASETS)
    saved_lakes = copy.deepcopy(LAKE_CONFIG["lakes"])
    yield
    TUSHARE_DATASETS.clear()
    TUSHARE_DATASETS.update(saved_datasets)
    LAKE_CONFIG["lakes"].clear()
    LAKE_CONFIG["lakes"].update(saved_lakes)


class _FakePro:
    """tushare pro 替身：按 api_name 返回可控 DataFrame（与 stock/etf 文件同实现）。"""
    def __init__(self):
        self._data = {}

    def set(self, api, df):
        self._data[api] = df

    def __getattr__(self, api):
        def _c(**kw):
            return self._data.get(api, pd.DataFrame())
        return _c


@pytest.fixture
def fake_pro(monkeypatch):
    """mock pro 接口 + 限频/熔断器（双 patch get_pro，与 stock/etf 文件同手法）。"""
    fake = _FakePro()
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: fake)
    monkeypatch.setattr("data.tushare_sync.get_pro", lambda: fake)
    monkeypatch.setattr("data.tushare_sync.tushare_rate_limiter",
                        type("L", (), {"acquire": lambda self, n: None})())
    monkeypatch.setattr("data.tushare_sync.tushare_breaker",
                        type("B", (), {"allow_request": lambda self: True,
                                       "record_success": lambda self: None,
                                       "record_failure": lambda self: None})())
    return fake


def test_macro_cpi_ppi_gdp_pmi_registered():
    """宏观指标 4 数据集配置完备性 + index_mode=datetime 契约。

    Why 守卫 index_mode：宏观湖是 DatetimeIndex（无 symbol 层），区别于股票湖的
    MultiIndex(date, symbol)。_sync_single 依据 index_mode='datetime' 分支重建时间索引；
    配置漏写 index_mode 会走原 single 路径落扁平 df（无时间索引），DataLakeReader 按日期
    切片直接 KeyError。配置层钉死。
    """
    macro_keys = ("cn_cpi", "cn_ppi", "cn_gdp", "cn_pmi")
    required_fields = ("api", "by", "date_col", "symbol_col", "fields", "lake",
                       "index_mode")
    for key in macro_keys:
        assert key in TUSHARE_DATASETS, f"{key} 未注册"
        cfg = TUSHARE_DATASETS[key]
        for f in required_fields:
            assert f in cfg, f"{key} 缺字段 {f}"
        assert cfg["by"] == "single", f"{key} by 应为 single（宏观无分页）"
        assert cfg["index_mode"] == "datetime", \
            f"{key} index_mode 必须为 datetime（宏观湖 DatetimeIndex）"
    # date_col 口径：月频 month / 季频 quarter（前视红线：宏观无 end_date，发布日即生效）
    assert TUSHARE_DATASETS["cn_gdp"]["date_col"] == "quarter", "cn_gdp 季频 date_col=quarter"
    for key in ("cn_cpi", "cn_ppi", "cn_pmi"):
        assert TUSHARE_DATASETS[key]["date_col"] == "month", f"{key} 月频 date_col=month"


def test_shibor_datasets_registered():
    """shibor/shibor_quote 配置完备性 + index_mode=datetime。"""
    for key in ("shibor", "shibor_quote"):
        assert key in TUSHARE_DATASETS, f"{key} 未注册"
        cfg = TUSHARE_DATASETS[key]
        assert cfg["by"] == "single", f"{key} by 应为 single"
        assert cfg["index_mode"] == "datetime", f"{key} index_mode 应为 datetime"
        assert cfg["date_col"] == "date", f"{key} date_col 应为 date"


def test_szse_sse_daily_by_date_registered():
    """szse_daily/sse_daily by=date 契约（市场级时序，date_col/symbol_col 均 trade_date）。"""
    for key in ("szse_daily", "sse_daily"):
        assert key in TUSHARE_DATASETS, f"{key} 未注册"
        cfg = TUSHARE_DATASETS[key]
        assert cfg["by"] == "date", f"{key} by 应为 date"
        assert cfg["date_col"] == "trade_date", f"{key} date_col 应为 trade_date"
        assert cfg["symbol_col"] == "trade_date", \
            f"{key} symbol_col 应为 trade_date（市场级，无个股 symbol）"
        # 不应有 index_mode（by=date 走 _build_multiindex，非 _sync_single）
        assert "index_mode" not in cfg, f"{key} by=date 不应声明 index_mode"


def test_macro_lakes_registered():
    """8 个宏观湖必须在 LAKE_CONFIG['lakes'] 注册且路径与 TUSHARE_DATASETS 一致。

    注：szse_daily/sse_daily 的 LAKE_CONFIG key 用数据集名（szse_daily/sse_daily），
    lake 路径用 mkt_daily_*.parquet——单一真相源 LAKE_CONFIG[key]==TUSHARE_DATASETS[key]['lake']。
    """
    for key in ("cn_cpi", "cn_ppi", "cn_gdp", "cn_pmi", "shibor", "shibor_quote",
                "szse_daily", "sse_daily"):
        assert key in LAKE_CONFIG["lakes"], f"{key} 未注册到 LAKE_CONFIG['lakes']"
        assert LAKE_CONFIG["lakes"][key] == TUSHARE_DATASETS[key]["lake"], \
            f"{key} LAKE_CONFIG 路径与 TUSHARE_DATASETS 不一致（单一真相源）"


def test_cn_cpi_lake_datetime_index(tmp_path, fake_pro, monkeypatch):
    """cn_cpi 宏观湖：DatetimeIndex（无 symbol 层），非 MultiIndex。

    Why 端到端契约：_sync_single + index_mode=datetime 必须把 month 列解析为
    pd.DatetimeIndex 并 set_index。月频 YYYYMM 格式需 format 推断（非 %Y%m%d）。
    """
    fake_pro.set("cn_cpi", pd.DataFrame({
        "month": ["202401", "202402"], "nt_yoy": [0.5, -0.3], "nt_mom": [0.1, 0.2]}))
    monkeypatch.setitem(TUSHARE_DATASETS["cn_cpi"], "lake",
                        str(tmp_path / "cpi.parquet"))
    import data.tushare_sync as ts
    ts.sync_dataset("cn_cpi", "2024-01-01", "2024-12-31", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["cn_cpi"]["lake"])
    assert isinstance(df.index, pd.DatetimeIndex), "cn_cpi 必须落 DatetimeIndex"
    assert df.index.name in ("month", "date"), f"索引名错误：{df.index.name}"
    assert "nt_yoy" in df.columns
    assert len(df) == 2


def test_cn_gdp_lake_datetime_index_quarter(tmp_path, fake_pro, monkeypatch):
    """cn_gdp 季频：quarter 列（YYYYQ1）解析为 DatetimeIndex（季末月首日）。

    Why 季度格式分支：YYYYQ1 非标准日期，_sync_single 需走 %YQ%q 季度解析
    （不能套 %Y%m）。本测试守卫季度解析分支不被月频逻辑吞掉。
    """
    fake_pro.set("cn_gdp", pd.DataFrame({
        "quarter": ["2024Q1", "2024Q2"],
        "gdp": [2.96e10, 3.17e10], "gdp_yoy": [5.3, 5.1]}))
    monkeypatch.setitem(TUSHARE_DATASETS["cn_gdp"], "lake",
                        str(tmp_path / "gdp.parquet"))
    import data.tushare_sync as ts
    ts.sync_dataset("cn_gdp", "2024-01-01", "2024-12-31", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["cn_gdp"]["lake"])
    assert isinstance(df.index, pd.DatetimeIndex), "cn_gdp 必须落 DatetimeIndex"
    assert len(df) == 2


def test_shibor_lake_datetime_index(tmp_path, fake_pro, monkeypatch):
    """shibor 日频：date 列解析为 DatetimeIndex。"""
    fake_pro.set("shibor", pd.DataFrame({
        "date": ["20240105", "20240108"],
        "on": [1.8, 1.85], "1w": [1.9, 1.92], "1y": [2.3, 2.31]}))
    monkeypatch.setitem(TUSHARE_DATASETS["shibor"], "lake",
                        str(tmp_path / "shibor.parquet"))
    import data.tushare_sync as ts
    ts.sync_dataset("shibor", "2024-01-01", "2024-12-31", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["shibor"]["lake"])
    assert isinstance(df.index, pd.DatetimeIndex), "shibor 必须落 DatetimeIndex"
    assert "1y" in df.columns


def test_szse_daily_by_date(tmp_path, fake_pro, monkeypatch):
    """szse_daily by=date：市场级时序，落 MultiIndex(date, symbol)，symbol=trade_date。

    Why monkeypatch _trade_days：by=date 走 _sync_by_date → _trade_days(start,end)，
    不 patch 会触达真实 trade_cal 网络。单日 mock 守卫 _build_multiindex 对
    symbol_col=trade_date 的处理（symbol 层恒等于 trade_date 字符串）。
    """
    import data.tushare_sync as ts
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])
    fake_pro.set("szse_daily", pd.DataFrame({
        "trade_date": ["20240105"], "issuer_num": [2000], "sec_num": [2500],
        "total_share": [2e12], "total_value": [3e12], "pe": [20.5]}))
    monkeypatch.setitem(TUSHARE_DATASETS["szse_daily"], "lake",
                        str(tmp_path / "szse.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["szse_daily"], "shard_dir",
                        str(tmp_path / "shards_szse"))
    ts.sync_dataset("szse_daily", "2024-01-05", "2024-01-05", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["szse_daily"]["lake"])
    assert df.index.names == ["date", "symbol"], "szse_daily 索引名错"
    # symbol 层恒等于 trade_date（市场级，无个股）
    assert "20240105" in df.index.get_level_values("symbol")
    assert "pe" in df.columns
