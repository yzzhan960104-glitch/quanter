# -*- coding: utf-8 -*-
"""数据完整性核心模块（data/integrity.py）单测。

覆盖阶段 1 两个基础组件：
- load_suspend_intervals：suspend_d 的 S/R 事件 → 按 symbol 重建停牌交易日集合
- fetch_trade_days：trade_cal → 指定区间的交易日集合（跨年合并 + 过滤）

停牌语义红线（A 股物理事实，算法必须遵守）：
- S（停牌）当日：标的停牌，无行情 → 算停牌日
- R（复牌）当日：标的恢复交易，有行情 → 不算停牌日（daily 湖应有这根）
- 停牌区间 = [S 日, R 日) 之间的交易日（含 S 不含 R）
- 未复牌（S 后无 R）：从 S 到最新交易日都算停牌
"""
from __future__ import annotations

import pandas as pd
import pytest


# ============================================================================
# 测试夹具：suspend_d 构造 + 交易日集
# ============================================================================

def _make_suspend_df(events):
    """构造 suspend_d 测试 DataFrame。

    events = [(date_str, symbol, suspend_type), ...]，suspend_type ∈ {"S","R"}。
    返回 MultiIndex(date, symbol) + suspend_type 列，对齐真实 suspend_d.parquet schema。
    """
    dates = [pd.Timestamp(d) for d, _, _ in events]
    syms = [s for _, s, _ in events]
    types = [t for _, _, t in events]
    return pd.DataFrame(
        {"suspend_type": types},
        index=pd.MultiIndex.from_arrays([dates, syms], names=["date", "symbol"]),
    )


# 测试用交易日集（2024-09 月初两周，剔除周末）
TRADE_DAYS = {
    "2024-09-02", "2024-09-03", "2024-09-04", "2024-09-05", "2024-09-06",
    "2024-09-09", "2024-09-10", "2024-09-11",
}


# ============================================================================
# load_suspend_intervals：S/R 事件 → 停牌交易日集合
# ============================================================================

def test_suspend_multi_day_range_excludes_recovery_day():
    """多日停牌：S(09-02) R(09-05) → 停牌日 {09-02,09-03,09-04}。

    R 当日（09-05）复牌有行情，不算停牌——这是 gate 区分「合法跳空」vs「漏采」的关键。
    """
    from data.integrity import load_suspend_intervals
    df = _make_suspend_df([
        ("2024-09-02", "000413.SZ", "S"),
        ("2024-09-05", "000413.SZ", "R"),
    ])
    intervals = load_suspend_intervals(df, TRADE_DAYS)
    assert intervals["000413.SZ"] == {"2024-09-02", "2024-09-03", "2024-09-04"}


def test_suspend_single_day():
    """单日停牌：S(09-02) R(09-03) → 停牌日 {09-02}。"""
    from data.integrity import load_suspend_intervals
    df = _make_suspend_df([
        ("2024-09-02", "000413.SZ", "S"),
        ("2024-09-03", "000413.SZ", "R"),
    ])
    intervals = load_suspend_intervals(df, TRADE_DAYS)
    assert intervals["000413.SZ"] == {"2024-09-02"}


def test_suspend_not_recovered_extends_to_max_trade_day():
    """未复牌：S(09-02) 无 R → 从 S 到 trade_days 最大日（09-11）全算停牌。

    物理场景：标的停牌中尚未复牌，lake 缺这些日属合法（停牌），不应误判漏采。
    """
    from data.integrity import load_suspend_intervals
    df = _make_suspend_df([("2024-09-02", "000413.SZ", "S")])
    intervals = load_suspend_intervals(df, TRADE_DAYS)
    assert intervals["000413.SZ"] == {
        "2024-09-02", "2024-09-03", "2024-09-04", "2024-09-05", "2024-09-06",
        "2024-09-09", "2024-09-10", "2024-09-11",
    }


def test_suspend_multiple_periods():
    """多个停牌周期：S1/R1 + S2/R2 → 两组区间都识别，互不干扰。"""
    from data.integrity import load_suspend_intervals
    df = _make_suspend_df([
        ("2024-09-02", "000413.SZ", "S"),
        ("2024-09-04", "000413.SZ", "R"),
        ("2024-09-09", "000413.SZ", "S"),
        ("2024-09-10", "000413.SZ", "R"),
    ])
    intervals = load_suspend_intervals(df, TRADE_DAYS)
    # 第1周期 [09-02, 09-04) = {09-02, 09-03}; 第2周期 [09-09, 09-10) = {09-09}
    assert intervals["000413.SZ"] == {"2024-09-02", "2024-09-03", "2024-09-09"}


def test_suspend_symbol_without_events_absent():
    """无停牌记录的标的：不在返回 dict（调用方 .get(sym, set()) 取空集）。"""
    from data.integrity import load_suspend_intervals
    df = _make_suspend_df([("2024-09-02", "000413.SZ", "S")])
    intervals = load_suspend_intervals(df, TRADE_DAYS)
    assert "000001.SZ" not in intervals  # 大盘股几乎不停牌，不在 dict


def test_suspend_multiple_symbols_isolated():
    """多标的：per-symbol 独立重建区间，不串味。"""
    from data.integrity import load_suspend_intervals
    df = _make_suspend_df([
        ("2024-09-02", "000413.SZ", "S"),
        ("2024-09-04", "000413.SZ", "R"),
        ("2024-09-09", "600777.SH", "S"),
        ("2024-09-10", "600777.SH", "R"),
    ])
    intervals = load_suspend_intervals(df, TRADE_DAYS)
    assert intervals["000413.SZ"] == {"2024-09-02", "2024-09-03"}
    assert intervals["600777.SH"] == {"2024-09-09"}


# ============================================================================
# fetch_trade_days：trade_cal → 区间交易日集合
# ============================================================================

def test_fetch_trade_days_aggregates_years_and_filters_range(monkeypatch):
    """跨年 fetch_trade_days：逐年 _fetch_year_trade_cal 合并 + 过滤到 [start, end]。

    封装 trading/calendar.fetch_trade_cal（逐年缓存），跨年区间需合并多年；
    边界含 start/end（闭区间，与 get_timeseries .loc[start:end] 同口径）。
    """
    from data import integrity
    monkeypatch.setattr(integrity, "_fetch_year_trade_cal", lambda y: {
        2023: ["2023-12-28", "2023-12-29", "2023-12-30"],  # 跨到 2023 末
        2024: ["2024-01-02", "2024-01-03", "2024-01-04"],
    }.get(y, []))
    days = integrity.fetch_trade_days("2023-12-29", "2024-01-03")
    # 含边界 12-29 / 01-03；过滤掉 12-28（早于 start）和 01-04（晚于 end）
    assert days == {"2023-12-29", "2023-12-30", "2024-01-02", "2024-01-03"}


# ============================================================================
# find_gaps：全市场连续性扫描（规则 1）
# ============================================================================

def _make_daily_df(rows):
    """构造 daily 湖测试 DataFrame（只关心 date/symbol 索引，OHLCV 值任意）。

    rows = [(date_str, symbol), ...]。返回 MultiIndex(date, symbol) + OHLCV 列。
    """
    dates = [pd.Timestamp(d) for d, _ in rows]
    syms = [s for _, s in rows]
    n = len(rows)
    return pd.DataFrame(
        {"open": range(n), "high": range(n), "low": range(n),
         "close": range(n), "volume": range(n)},
        index=pd.MultiIndex.from_arrays([dates, syms], names=["date", "symbol"]),
    )


def test_find_gaps_detects_unjustified_missing_segment():
    """漏采：标的 [min,max] 区间缺中间交易日且非停牌 → 1 个 unjustified GapRange。

    300214.SZ 案例的核心：缺 07-14~07-21 非停牌 → 应被识别为漏采（suspend_justified=False）。
    """
    from data.integrity import find_gaps
    df = _make_daily_df([
        ("2024-09-02", "000001.SZ"), ("2024-09-03", "000001.SZ"),
        ("2024-09-06", "000001.SZ"), ("2024-09-09", "000001.SZ"),  # 缺 09-04, 09-05
    ])
    gaps = find_gaps(df, TRADE_DAYS, suspend_intervals={})
    assert len(gaps) == 1
    g = gaps[0]
    assert g.symbol == "000001.SZ"
    assert g.missing_dates == ("2024-09-04", "2024-09-05")
    assert g.suspend_justified is False


def test_find_gaps_no_gap_when_complete():
    """完整：标的区间内交易日齐全 → []。"""
    from data.integrity import find_gaps
    df = _make_daily_df([(d, "000001.SZ") for d in
                         ["2024-09-02", "2024-09-03", "2024-09-04", "2024-09-05", "2024-09-06"]])
    assert find_gaps(df, TRADE_DAYS, suspend_intervals={}) == []


def test_find_gaps_suspend_period_justified():
    """停牌跳空：缺日全在 suspend 区间 → GapRange(suspend_justified=True)，不算漏采。

    物理场景：标的 09-03/09-04 停牌（无行情合法），lake 缺这两日属正常，不应触发补采。
    """
    from data.integrity import find_gaps
    df = _make_daily_df([
        ("2024-09-02", "000413.SZ"),  # 停牌前最后一根
        ("2024-09-05", "000413.SZ"),  # 复牌后第一根（09-03/09-04 停牌缺）
        ("2024-09-06", "000413.SZ"),
    ])
    susp = {"000413.SZ": {"2024-09-03", "2024-09-04"}}
    gaps = find_gaps(df, TRADE_DAYS, suspend_intervals=susp)
    assert len(gaps) == 1
    assert set(gaps[0].missing_dates) == {"2024-09-03", "2024-09-04"}
    assert gaps[0].suspend_justified is True


# ============================================================================
# check_window_continuity：窗口连续性检查（规则 4 scan_live gate 用）
# ============================================================================

def _make_window_df(dates):
    """单标的窗口 df（DatetimeIndex，对齐 scan_live 的 df_upto.tail(window)）。"""
    n = len(dates)
    return pd.DataFrame(
        {"open": range(n), "high": range(n), "low": range(n),
         "close": range(n), "volume": range(n)},
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in dates], name="date"),
    )


def test_check_window_ok_when_complete():
    """窗口完整（含全部应有交易日）→ ok=True。"""
    from data.integrity import check_window_continuity
    win = _make_window_df(["2024-09-02", "2024-09-03", "2024-09-04", "2024-09-05", "2024-09-06"])
    r = check_window_continuity(win, TRADE_DAYS, suspend_intervals={}, symbol="000001.SZ")
    assert r.ok is True
    assert r.unjustified == ()


def test_check_window_fails_on_unjustified_gap():
    """窗口缺日且非停牌 → ok=False（gate 据此跳过标的），unjustified 列出漏采日。"""
    from data.integrity import check_window_continuity
    win = _make_window_df(["2024-09-02", "2024-09-03", "2024-09-06"])  # 缺 09-04, 09-05
    r = check_window_continuity(win, TRADE_DAYS, suspend_intervals={}, symbol="000001.SZ")
    assert r.ok is False
    assert set(r.unjustified) == {"2024-09-04", "2024-09-05"}


def test_check_window_ok_when_gap_all_suspend():
    """窗口缺日但全是停牌 → ok=True（合法跳空，不触发 gate）。

    gate 红线：停牌造成的跳空是合法的，不能因停牌误跳过标的（否则停牌复牌标的永不被识别）。
    """
    from data.integrity import check_window_continuity
    win = _make_window_df(["2024-09-02", "2024-09-05", "2024-09-06"])  # 缺 09-03, 09-04（停牌）
    susp = {"000413.SZ": {"2024-09-03", "2024-09-04"}}
    r = check_window_continuity(win, TRADE_DAYS, suspend_intervals=susp, symbol="000413.SZ")
    assert r.ok is True
    assert set(r.missing_dates) == {"2024-09-03", "2024-09-04"}
    assert r.unjustified == ()


# ============================================================================
# filter_universe_by_continuity：universe 级完整性 gate（规则 4 · Task 7 U5 gate 下沉）
# ============================================================================
# 物理意图（300214.SZ 漏采教训 · memory data-lake-integrity-gap）：
#   原完整性 gate 内联在 strategies/neckline_method.scan_live（per-symbol 自验窗口连续性），
#   导致「策略层混入数据质量代码」+ 「回测/实盘各走各的 gate」。Task 7 把 gate 上提到
#   data/integrity 的 universe 级纯函数：调用方（_eod / replay）先 filter universe，策略层
#   scan_live 假设已过滤——回测/实盘共用同一 filter（数据校验单源）。
#
# strangler 红线：filter 逻辑零改动于 scan_live 原内联 gate——同样调 check_window_continuity，
# 只从 per-symbol 上提到 universe 级 pre-filter。本测试覆盖：
#   1. 漏采 symbol 过滤（窗口含未解释缺日 → 不在 clean_universe）
#   2. 干净 symbol 保留（窗口完整 → 在 clean_universe）
#   3. 全停牌跳空放行（合法跳空 → 在 clean_universe）
#   4. fail-open 放行（susp={}/trade_days=set() = 加载失败 → 全放行，退回原行为）


def test_filter_universe_drops_unjustified_gap_symbol():
    """漏采 symbol（窗口缺日且非停牌）→ 被 filter 过滤（不在 clean_universe）。

    等价原 scan_live gate 的「漏采 return []」：漏采 symbol 不进 clean_universe，
    调用方据此跳过该 symbol 的 scan_live（不产误信号，300214.SZ 教训）。
    """
    from data.integrity import filter_universe_by_continuity
    universe = ["000001.SZ", "000002.SZ"]
    # 000001.SZ 窗口完整；000002.SZ 窗口缺 09-04/09-05 且非停牌（漏采）
    df_map = {
        "000001.SZ": _make_window_df(["2024-09-02", "2024-09-03", "2024-09-04",
                                      "2024-09-05", "2024-09-06"]),
        "000002.SZ": _make_window_df(["2024-09-02", "2024-09-03", "2024-09-06"]),
    }
    clean = filter_universe_by_continuity(
        universe, df_map, window=5, susp={}, trade_days=TRADE_DAYS)
    assert "000001.SZ" in clean, "完整窗口 symbol 应保留"
    assert "000002.SZ" not in clean, "漏采 symbol 应被过滤"


def test_filter_universe_keeps_all_suspend_gap_symbol():
    """窗口缺日但全停牌（合法跳空）→ 保留（不能因停牌误过滤）。

    gate 红线：停牌跳空是合法的，filter 不能误剔除（否则停牌复牌标的永不被回测/识别）。
    """
    from data.integrity import filter_universe_by_continuity
    universe = ["000413.SZ"]
    df_map = {"000413.SZ": _make_window_df(["2024-09-02", "2024-09-05", "2024-09-06"])}
    susp = {"000413.SZ": {"2024-09-03", "2024-09-04"}}
    clean = filter_universe_by_continuity(
        universe, df_map, window=3, susp=susp, trade_days=TRADE_DAYS)
    assert clean == ["000413.SZ"], "全停牌跳空是合法的，应保留"


def test_filter_universe_failopen_when_trade_days_empty():
    """fail-open：trade_days 为空集（加载失败/测试降级）→ 全放行。

    与原 scan_live:229 `if _td:` fail-open 同口径——gate 是新增防护，加载失败时
    退回原行为（全放行），不阻断识别（与 reader 离线降级同口径）。
    """
    from data.integrity import filter_universe_by_continuity
    universe = ["000001.SZ", "000002.SZ"]
    # 即使 000002.SZ 缺日，trade_days 空集 → 无法判定漏采 → 全放行
    df_map = {
        "000001.SZ": _make_window_df(["2024-09-02", "2024-09-03"]),
        "000002.SZ": _make_window_df(["2024-09-02", "2024-09-06"]),
    }
    clean = filter_universe_by_continuity(
        universe, df_map, window=2, susp={}, trade_days=set())
    assert set(clean) == {"000001.SZ", "000002.SZ"}, "fail-open：trade_days 空集应全放行"


def test_filter_universe_preserves_input_order():
    """clean_universe 保持 universe 输入顺序（调用方遍历顺序稳定，便于断言/复现）。

    物理意图：_eod / replay 遍历 clean_universe 调 scan_live，顺序稳定让 A2 信号对照
    可复现；dict 保留 universe 顺序（Python 3.7+ dict 有序，list comp 保持顺序）。
    """
    from data.integrity import filter_universe_by_continuity
    universe = ["600000.SH", "000001.SZ", "300001.SZ"]
    df_map = {s: _make_window_df(["2024-09-02", "2024-09-03", "2024-09-04"]) for s in universe}
    clean = filter_universe_by_continuity(
        universe, df_map, window=3, susp={}, trade_days=TRADE_DAYS)
    assert clean == universe, "filter 应保持 universe 输入顺序"


def test_filter_universe_skips_symbol_missing_from_df_map():
    """df_map 缺该 symbol（df_upto 加载失败）→ 过滤掉（不进 clean_universe）。

    边界：df_map.get(sym) 返 None 时不应抛错（调用方 _eod 的 _load_df_upto 可能返 None），
    安全跳过并记 warning（与原 scan_live 调用前 _eod:1206 的 None 跳过同口径）。
    """
    from data.integrity import filter_universe_by_continuity
    universe = ["000001.SZ", "000002.SZ"]
    df_map = {"000001.SZ": _make_window_df(["2024-09-02", "2024-09-03", "2024-09-04"])}
    # 000002.SZ 不在 df_map
    clean = filter_universe_by_continuity(
        universe, df_map, window=3, susp={}, trade_days=TRADE_DAYS)
    assert clean == ["000001.SZ"], "df_map 缺失的 symbol 应被过滤"
