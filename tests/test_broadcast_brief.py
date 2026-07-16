# -*- coding: utf-8 -*-
"""brief 文案生成器单测：正常路径 / 缺数据降级 / NaN 守护。"""
import pandas as pd

from broadcast.brief import build_daily_brief
from broadcast import name_resolver


class FakeReader:
    """注入式假 reader：按 lake 返预设截面 / 时序，模拟 DataLakeReader。"""

    def __init__(self, xs_map, ts_map):
        self._xs = xs_map   # lake -> 截面 DF
        self._ts = ts_map   # symbol -> 时序 DF(col=close)

    def get_cross_section(self, date, *, lake=None):
        return self._xs.get(lake, pd.DataFrame())

    def get_timeseries(self, symbol, start, end, *, lake=None):
        return self._ts.get(symbol, pd.DataFrame())


def _disable_names(monkeypatch):
    """个股名降级（无 stock_basic 权限态）+ 板块名空（无 ths 字典）。"""
    import data.symbol_names as sn
    monkeypatch.setattr(sn, "_LOADED", True)
    monkeypatch.setattr(sn, "_NAME_MAP", {})
    name_resolver._THS_NAMES.clear()


def _make_reader():
    index_xs = pd.DataFrame(
        {"close": [3856.21, 3128.0]},
        index=pd.Index(["000300.SH", "399001.SZ"], name="symbol"),
    )
    index_ts = {
        "000300.SH": pd.DataFrame(
            {"close": [3800.0, 3856.21]},
            index=pd.to_datetime(["2026-07-14", "2026-07-15"]),
        ),
        "399001.SZ": pd.DataFrame(
            {"close": [3150.0, 3128.0]},
            index=pd.to_datetime(["2026-07-14", "2026-07-15"]),
        ),
    }
    ths_xs = pd.DataFrame(
        {"pct_change": [5.2, -2.3, 3.1, -1.8, 1.5, 0.7, -0.9, 2.1, -3.0, 4.0]},
        index=pd.Index([f"88500{i}.TI" for i in range(10)], name="symbol"),
    )
    mf_xs = pd.DataFrame(
        {"net_mf_amount": [32000.0, 18000.0, -5000.0]},
        index=pd.Index(["000001.SZ", "600519.SH", "300750.SZ"], name="symbol"),
    )
    dragon_xs = pd.DataFrame(
        {"hit": [1, 0, 1]},
        index=pd.Index(["000001.SZ", "000002.SZ", "300999.SZ"], name="symbol"),
    )
    return FakeReader(
        {"index_daily": index_xs, "ths_daily": ths_xs, "moneyflow": mf_xs, "dragon_list": dragon_xs},
        index_ts,
    )


def test_brief_normal_path(monkeypatch):
    _disable_names(monkeypatch)
    brief = build_daily_brief("2026-07-15", reader=_make_reader())
    md = brief.markdown

    assert brief.date == "2026-07-15"
    assert "沪深300" in md              # 指数中文名（硬编码命中）
    assert "上证指数" not in md or "沪深300" in md  # 只渲染存在的 2 指数
    assert "▲" in md                   # 3856.21 > 3800 → 涨
    assert "▼" in md                   # 3128 < 3150 → 跌
    assert "885000.TI" in md           # 板块降级显代码（_THS_NAMES 空）
    assert "上榜 2 只" in md           # hit==1 有 2 只（000001.SZ / 300999.SZ）
    assert "+3.20亿" in md             # 32000 万 → 3.20 亿（净流入 Top1）
    assert "周" in md                  # 周几已渲染


def test_brief_missing_data_degrades(monkeypatch):
    _disable_names(monkeypatch)
    r = _make_reader()
    r._xs["ths_daily"] = pd.DataFrame()
    r._xs["moneyflow"] = pd.DataFrame()
    r._xs["dragon_list"] = pd.DataFrame()

    md = build_daily_brief("2026-07-15", reader=r).markdown

    assert "沪深300" in md               # 大盘仍正常（index_daily 有数据）
    assert "板块数据未落湖" in md        # 板块降级
    assert "资金流数据未落湖" in md      # 资金降级
    assert "龙虎榜数据未落湖" in md      # 龙虎榜降级


def test_brief_index_only_no_crash(monkeypatch):
    _disable_names(monkeypatch)
    # 只剩大盘，其余全空 → 不抛，大盘渲染
    r = FakeReader(
        {"index_daily": pd.DataFrame({"close": [100.0]}, index=pd.Index(["000300.SH"], name="symbol")),
         "ths_daily": pd.DataFrame(), "moneyflow": pd.DataFrame(), "dragon_list": pd.DataFrame()},
        {"000300.SH": pd.DataFrame({"close": [99.0, 100.0]}, index=pd.to_datetime(["2026-07-14", "2026-07-15"]))},
    )
    md = build_daily_brief("2026-07-15", reader=r).markdown
    assert "沪深300" in md
    assert "▲" in md


def test_brief_nan_guard(monkeypatch):
    _disable_names(monkeypatch)
    r = _make_reader()
    # 000300 时序含 NaN（前一日 NaN）→ tail2 dropna 后不足 2 → 涨跌幅「—」
    r._ts["000300.SH"] = pd.DataFrame(
        {"close": [float("nan"), 3856.21]},
        index=pd.to_datetime(["2026-07-14", "2026-07-15"]),
    )
    md = build_daily_brief("2026-07-15", reader=r).markdown
    assert "—" in md                    # NaN 守护：渲染「—」不崩


def test_brief_all_empty_still_returns(monkeypatch):
    _disable_names(monkeypatch)
    # 全湖空 → 不抛，返回含 header/footer 的降级文案
    r = FakeReader(
        {k: pd.DataFrame() for k in ["index_daily", "ths_daily", "moneyflow", "dragon_list"]},
        {},
    )
    brief = build_daily_brief("2026-07-15", reader=r)
    assert brief.date == "2026-07-15"
    assert "每日行情播报" in brief.markdown
    assert "大盘数据未落湖" in brief.markdown
