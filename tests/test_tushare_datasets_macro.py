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
- **交易所日级统计**：mkt_daily（B 类合并：szse/sse → daily_info）by=date（市场级时序，
  symbol_col=trade_date，symbol 层恒等于交易日），落 MultiIndex(date, symbol)，区别于宏观
  single 的 DatetimeIndex。

fixture 复制说明：与 test_tushare_datasets_etf.py 同——完整复制 autouse
_isolate_tushare_registry + fake_pro fixture（conftest 未抽取，文件级作用域）。

注：mkt_daily 的 LAKE_CONFIG key 与 TUSHARE_DATASETS key 一致（mkt_daily），lake 路径
mkt_daily.parquet（单一真相源：LAKE_CONFIG[key]==TUSHARE_DATASETS[key]['lake']）。
"""
import pandas as pd
import pytest

from config import TUSHARE_DATASETS, LAKE_CONFIG


@pytest.fixture(autouse=True)
def _isolate_tushare_registry(tushare_registry_isolated):
    """薄壳（原 ~40 行块收口 tests/conftest.py::tushare_registry_isolated，2026-08-19 W2）。"""

@pytest.fixture
def fake_pro(monkeypatch):
    """薄壳（原 ~60 行块收口 tests/_tushare_stub.py，2026-08-19 W2）。"""
    from tests._tushare_stub import FakePro, install_fake_pro
    fake = FakePro(data=None)
    install_fake_pro(monkeypatch, fake)
    return fake


def test_cn_cpi_lake_datetime_index(tmp_path, fake_pro, monkeypatch):
    """cn_cpi 宏观湖：DatetimeIndex（无 symbol 层），非 MultiIndex。

    Why 端到端契约：_sync_single + index_mode=datetime 必须把 month 列解析为
    pd.DatetimeIndex 并 set_index。月频 YYYYMM 格式需 format 推断（非 %Y%m%d）。

    ⚠️ 真实列对齐：删幻觉 yty_yoy（真实城镇同比为 town_yoy），含 nt_val/town_yoy。
    """
    fake_pro.set("cn_cpi", pd.DataFrame({
        "month": ["202401", "202402"], "nt_val": [100.5, 99.7],
        "nt_yoy": [0.5, -0.3], "nt_mom": [0.1, 0.2], "town_yoy": [0.6, -0.2]}))
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


def test_mkt_daily_by_date(tmp_path, fake_pro, monkeypatch):
    """mkt_daily（daily_info）by=date：市场级时序，落 MultiIndex(date, symbol)，symbol=trade_date。

    Why monkeypatch _trade_days：by=date 走 _sync_by_date → _trade_days(start,end)，
    不 patch 会触达真实 trade_cal 网络。单日 mock 守卫 _build_multiindex 对
    symbol_col=trade_date 的处理（symbol 层恒等于 trade_date 字符串）。

    ⚠️ 真实列对齐：daily_info 返 trade_date/ts_code/ts_name/com_count/total_share/
    float_share/total_mv/float_mv/pe/exchange 等（沪深两市，exchange 列区分）。
    """
    import data.tushare_sync as ts
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])
    # daily_info：单日返沪深两市两条（exchange 区分）
    fake_pro.set("daily_info", pd.DataFrame({
        "trade_date": ["20240105", "20240105"], "ts_code": ["SSE", "SZSE"],
        "ts_name": ["上海", "深圳"], "com_count": [2000, 2500],
        "total_share": [4e12, 2e12], "float_share": [3.5e12, 1.8e12],
        "total_mv": [5e13, 3e13], "float_mv": [4.5e13, 2.5e13],
        "pe": [15.5, 20.5], "exchange": ["SSE", "SZSE"]}))
    monkeypatch.setitem(TUSHARE_DATASETS["mkt_daily"], "lake",
                        str(tmp_path / "mkt_daily.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["mkt_daily"], "shard_dir",
                        str(tmp_path / "shards_mkt"))
    ts.sync_dataset("mkt_daily", "2024-01-05", "2024-01-05", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["mkt_daily"]["lake"])
    assert df.index.names == ["date", "symbol"], "mkt_daily 索引名错"
    # symbol 层恒等于 trade_date（市场级，无个股）
    assert "20240105" in df.index.get_level_values("symbol")
    assert "pe" in df.columns
    assert "com_count" in df.columns


# ---------------------------------------------------------------------------
# Plan C Task 2：sync_macro_credit 切 Tushare cn_m + CreditRegime 列名契约
# ---------------------------------------------------------------------------
# Why 本组测试：sync_macro_credit 从 akshare 切到 Tushare cn_m(M0/M1/M2)，社融/
# DR007 走 akshare fallback。CreditRegime(core/macro_regime.py:154) **不改**——
# 它只读 macro 湖列名 shrzgm + M1M2_gap（dr007 可选）。本组测试钉死"列名契约"：
# 源切换后产出湖必须仍含这两列，否则 CreditRegime 状态机返 0（中性兜底）导致
# 宏观 CTA 宏观锚失效，是整个四级数据湖顶层最致命的回归。


def test_macro_lake_credit_regime_columns(tmp_path, fake_pro, monkeypatch):
    """macro 湖必须含 shrzgm + M1M2_gap（CreditRegime core 字段契约）。

    Why 端到端契约：sync_macro_credit 重写为 Tushare cn_m(M1/M2) + akshare
    fallback(社融 shrzgm/DR007) 后，落盘的 macro_credit.parquet 必须仍含
    shrzgm + M1M2_gap 两列——CreditRegime(core 字段) 直接消费它们，缺失则
    compute() 走"缺列防御"分支返 0（强制中性），宏观否决/绿灯双双失效。
    M1M2_gap = M1同比 - M2同比（货币活性剪刀差，CreditRegime 据此判宽/紧信用）。
    """
    # Tushare cn_m：M0/M1/M2 同比（月频，month=YYYYMM）
    # ⚠️ 事实风险：cn_m 字段名(m0_yoy/m1_yoy/m2_yoy)与参数(start_m/end_m)待真 token 探测，
    #    单测用 fake_pro mock 不验证真实字段；本测试按 brief 约定字段名钉死契约。
    fake_pro.set("cn_m", pd.DataFrame({
        "month": ["202401", "202402"],
        "m0_yoy": [8.0, 8.5], "m1_yoy": [5.9, 6.6], "m2_yoy": [8.7, 8.7]}))
    # akshare 社融/DR007 fallback（mock：避开真实网络）
    import data.clients.akshare_client as akc
    monkeypatch.setattr(akc.AKShareClient, "fetch_macro_raw",
                        lambda self, kind: {
                            "shrzgm": pd.DataFrame({"月份": ["202401", "202402"],
                                                    "社融增量": [50000, 60000]}),
                            "dr007": pd.DataFrame({"日期": ["2024-01-05", "2024-02-05"],
                                                   "DR007": [1.9, 1.8]}),
                        }.get(kind, pd.DataFrame()))
    out = str(tmp_path / "macro.parquet")
    from data.tools.sync_macro_credit import sync_macro
    sync_macro("2024-01-01", "2024-02-28", out=out)
    df = pd.read_parquet(out)
    assert "shrzgm" in df.columns, "CreditRegime core 字段 shrzgm 缺失"
    assert "M1M2_gap" in df.columns, "CreditRegime core 字段 M1M2_gap 缺失"
    # M1M2_gap = M1同比 - M2同比：必须存在非空值（202402 期 6.6-8.7=-2.1）
    assert df["M1M2_gap"].notna().any()


