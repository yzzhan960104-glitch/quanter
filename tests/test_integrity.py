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

from data.integrity import (
    WRITE_GUARD_MIN_RATIO, WriteGuardError,
    assert_safe_overwrite,
    existing_row_count, check_row_count_drop,
)


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

# 长窗交易日集（连续 3 周 15 日）：市场共识启发式用例需 ≥10 日的长洞段
TRADE_DAYS_LONG = {
    "2024-09-02", "2024-09-03", "2024-09-04", "2024-09-05", "2024-09-06",
    "2024-09-09", "2024-09-10", "2024-09-11", "2024-09-12", "2024-09-13",
    "2024-09-16", "2024-09-17", "2024-09-18", "2024-09-19", "2024-09-20",
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
# 日级判定 + 长洞市场共识启发式（N1 停牌真值 · 2026-08-16）
# ============================================================================
# 物理意图（段级 all() 的放大效应，2026-08-16 实跑勘定）：
#   旧实现 `justified = all(d in susp for d in seg)`——段内任一日缺 S 事件 → 整段
#   误判漏采。而 suspend_d 2019 年后对长停牌只记零星 S（000670.SZ 589 天洞仅
#   11 行 S），导致 16,371 段 unjustified 误报。修复两级：
#   1. 日级判定：段拆「justified 日集 / unjustified 日集」，repair 只补真缺日；
#   2. 长洞市场共识启发式（≥10 交易日 + 湖在场数健康 + 段前后有数据）：
#      判 suspend_suspected=True（justified-with-flag）。


def test_find_gaps_day_level_splits_mixed_segment():
    """日级判定：段内 3 缺日中 2 日有 S 事件 → 只剩 1 日算 unjustified_days。

    旧段级 all() 会把整段（含 2 个已由 suspend_d 解释的日）全判漏采——
    000670.SZ 589 天洞仅 11 行 S 的形态下，578 个无记录日拖累 11 个有记录日。
    """
    from data.integrity import find_gaps
    df = _make_daily_df([
        ("2024-09-02", "000670.SZ"),
        ("2024-09-06", "000670.SZ"),  # 缺 09-03/09-04/09-05；前两日有 S、第三日无记录
        ("2024-09-09", "000670.SZ"),
    ])
    susp = {"000670.SZ": {"2024-09-03", "2024-09-04"}}
    gaps = find_gaps(df, TRADE_DAYS, suspend_intervals=susp)
    assert len(gaps) == 1
    g = gaps[0]
    assert g.suspend_justified is False, "段内仍有 1 日无解释 → 整段非全停牌"
    assert set(g.missing_dates) == {"2024-09-03", "2024-09-04", "2024-09-05"}
    assert g.unjustified_days == ("2024-09-05",), "只有无 S 记录的日进入 unjustified_days"
    assert g.suspend_suspected is False


def test_find_gaps_long_hole_market_consensus_suspected():
    """长洞启发式：≥10 日洞 + 段内每日湖在场数 ≥ 窗口中位数×0.8 → 停牌推定。

    000670.SZ 实证形态：589 天洞窗内每日湖在场 3,662-4,684 标的、零日低于中位数
    80%——市场一直健康而唯独该标的无数据，物理上只能是停牌（Tushare suspend_d
    2019 后长停牌稀疏化漏记）。判 suspend_suspected=True（合法跳空，不触发补采）。
    """
    from data.integrity import find_gaps
    days = sorted(TRADE_DAYS_LONG)
    hole = days[2:14]  # 12 个连续交易日洞（≥10 门槛）
    df = _make_daily_df([(d, "000670.SZ") for d in [days[0], days[1], days[14]]])
    presence = {d: 4000 for d in hole}  # 市场每日在场数健康且稳定
    gaps = find_gaps(df, TRADE_DAYS_LONG, suspend_intervals={},
                     market_presence=presence)
    assert len(gaps) == 1
    g = gaps[0]
    assert g.suspend_justified is True, "市场共识推定停牌 → 合法跳空"
    assert g.suspend_suspected is True, "须带 flag（与 suspend_d 铁证区分，scan 单列计数）"
    assert g.unjustified_days == ()


def test_find_gaps_short_segment_stays_strict_even_with_healthy_market():
    """短段（<10 日）不走启发式，维持严格 suspend_d 判定。

    风险权衡：2017-2018 suspend_d 密集覆盖期（2018 年 42,976 行 S），短洞真漏采
    概率高——误把真缺判停牌 = 数据永缺（repair 永不补），误放真缺 = repair 多拉
    一轮空（下轮 unfillable 标记收敛）。不对称代价下短段从严。
    """
    from data.integrity import find_gaps
    days = sorted(TRADE_DAYS_LONG)
    hole = days[2:7]  # 5 日短洞
    df = _make_daily_df([(d, "000001.SZ") for d in [days[0], days[1], days[7]]])
    presence = {d: 4000 for d in hole}
    gaps = find_gaps(df, TRADE_DAYS_LONG, suspend_intervals={},
                     market_presence=presence)
    assert len(gaps) == 1
    assert gaps[0].suspend_justified is False
    assert gaps[0].suspend_suspected is False
    assert gaps[0].unjustified_days == tuple(hole)


def test_find_gaps_heuristic_rejects_lake_outage_day():
    """启发式反例：段内某日湖在场数骤低（< 中位数×0.8）→ 不推定停牌。

    该日湖自身残缺（全市场漏采/同步事故），个股缺失可能是被湖故障连坐——
    此时推定停牌会把真缺日永久合法化，方向错误。
    """
    from data.integrity import find_gaps
    days = sorted(TRADE_DAYS_LONG)
    hole = days[2:14]
    df = _make_daily_df([(d, "000670.SZ") for d in [days[0], days[1], days[14]]])
    presence = {d: 4000 for d in hole}
    presence[hole[5]] = 100  # 该日湖在场数崩到 100（< 4000×0.8=3200）
    gaps = find_gaps(df, TRADE_DAYS_LONG, suspend_intervals={},
                     market_presence=presence)
    assert gaps[0].suspend_justified is False
    assert gaps[0].suspend_suspected is False


def test_find_gaps_heuristic_rejects_thin_market_era():
    """启发式反例：湖在场数中位数低于分母下限（2017 前仅 ~20 标的）→ 不可用。

    2017 前湖只有 ~20 个先锋标的在场——「市场共识」分母残缺，20 vs 16 的波动
    无统计意义，任何结论都是噪声。启发式只对 2017+（中位数 ≥2,908）有效。
    """
    from data.integrity import find_gaps
    days = sorted(TRADE_DAYS_LONG)
    hole = days[2:14]
    df = _make_daily_df([(d, "000670.SZ") for d in [days[0], days[1], days[14]]])
    presence = {d: 20 for d in hole}  # 2017 前形态：每日仅 ~20 标的在场面
    gaps = find_gaps(df, TRADE_DAYS_LONG, suspend_intervals={},
                     market_presence=presence)
    assert gaps[0].suspend_justified is False
    assert gaps[0].suspend_suspected is False


def test_find_gaps_heuristic_off_without_market_presence():
    """market_presence 未喂入（默认 None）→ 启发式关闭，退回严格 suspend_d 判定。

    兼容红线：既有调用方（sync_daily_incremental._backscan_recent 近窗回扫）不传
    在场计数，行为必须与旧版一致——启发式是 scan 全市场路径的可选增强，不是
    破坏性默认。
    """
    from data.integrity import find_gaps
    days = sorted(TRADE_DAYS_LONG)
    df = _make_daily_df([(d, "000670.SZ") for d in [days[0], days[1], days[14]]])
    gaps = find_gaps(df, TRADE_DAYS_LONG, suspend_intervals={})  # 不传 market_presence
    assert gaps[0].suspend_justified is False
    assert gaps[0].suspend_suspected is False


def test_heuristic_requires_data_before_and_after():
    """启发式前置：标的段前后均有数据（洞夹在真实行情之间）才推定停牌。

    退市末段/上市初段形态下，「之后无数据」可能是永久退市而非停牌——不适用
    市场共识推定（find_gaps 的 [amin,amax] 边界结构上保证此条件，本用例直测
    内核定防未来重构破坏）。
    """
    from data.integrity import _is_suspend_suspected
    seg = sorted(TRADE_DAYS_LONG)[2:14]
    pres = {d: 4000 for d in seg}
    assert _is_suspend_suspected(seg, pres, has_data_before=True, has_data_after=True)
    assert not _is_suspend_suspected(seg, pres, has_data_before=False, has_data_after=True)
    assert not _is_suspend_suspected(seg, pres, has_data_before=True, has_data_after=False)


def test_gaprange_legacy_construction_defaults_unjustified_days():
    """旧构造兼容：只传 5 字段的 GapRange（旧报告 JSON/旧测试）不破。

    suspend_justified=False 的段隐含 unjustified_days = 全部 missing_dates
    （与旧段级 all() 语义等价——repair 仍按整段补，不空转）；justified 段为空。
    """
    from data.integrity import GapRange
    g = GapRange("000001.SZ", "2024-09-04", "2024-09-05",
                 ("2024-09-04", "2024-09-05"), suspend_justified=False)
    assert g.unjustified_days == ("2024-09-04", "2024-09-05")
    assert g.suspend_suspected is False
    j = GapRange("000413.SZ", "2024-09-03", "2024-09-04",
                 ("2024-09-03", "2024-09-04"), suspend_justified=True)
    assert j.unjustified_days == ()
    assert j.suspend_suspected is False


def test_unjustified_subsegments_splits_consecutive_runs():
    """子段拆分：unjustified_days 按段内连续性拆 run——repair 以子段为单位。

    段内 8 日，unjustified 为 {d0,d1,d4}（d2/d3/d5.. 被 suspend_d 解释）→
    两个子段 (d0,d1) 与 (d4)：中间隔着被解释日，物理上是两处独立漏采。
    """
    from data.integrity import GapRange, unjustified_subsegments
    seg = tuple(sorted(TRADE_DAYS)[:8])
    g = GapRange("000001.SZ", seg[0], seg[-1], seg, suspend_justified=False,
                 unjustified_days=(seg[0], seg[1], seg[4]))
    assert unjustified_subsegments(g) == [(seg[0], seg[1]), (seg[4],)]


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


# ============================================================================
# 写入前历史行数守卫 · 行数检查 SSoT 纯函数（T13-A · Task 1）
# ============================================================================
# 物理意图（T12 实证）：通用同步器 to_parquet 直接覆盖、无写入前守卫 → a_shares_daily
# 被残片覆盖（1020万→3200）。本组测试覆盖「行数骤降判定内核」+ parquet 行数元数据读取，
# 供写入守卫（Task 2）与 freshness 行数骤降（Task 6）共用（SSoT，禁止两套实现）。


def test_write_guard_min_ratio_default_is_09():
    # 蓝图级默认阈值：新行数 < 现有 × 0.9 → 视为骤降拒写
    assert WRITE_GUARD_MIN_RATIO == 0.9


@pytest.mark.parametrize(
    "baseline, new, expected_ok, label",
    [
        # T12 场景：1020万 → 3200，新行数远小于基线 × 0.9 → 判骤降（事故锚）
        (10_000_000, 3200, False, "crater"),
        # 正常增量/增长：新行数 >= 基线 → 放行
        (1000, 1005, True, "growth"),
        # 边界：新行数 = baseline × 0.9 → 刚好放行（>= ratio 不算骤降）
        (1000, 900, True, "boundary_at_ratio"),
        # 边界：新行数 = baseline × 0.9 - 1 → 拒写
        (1000, 899, False, "boundary_below_ratio"),
    ],
)
def test_check_row_count_drop_boundaries(baseline, new, expected_ok, label):
    """行数骤降守卫四边界（原 4 个同构用例参数化合并，2026-08-19 W3）。

    物理意图：T12 事故（a_shares_daily 1020万→3200 被残片覆盖）后所有湖写入口
    的前置防线——新行数相对基线跌破 min_ratio 即拒写。四例覆盖事故值/增长/恰在
    界上/恰在界下，>= 与 > 的边界语义由此钉死。
    """
    ok, reason = check_row_count_drop(baseline=baseline, new=new, min_ratio=0.9)
    assert ok is expected_ok, f"{label}: 期望 {'放行' if expected_ok else '拒写'}，实际 reason={reason}"
    if not expected_ok:
        assert "骤降" in reason or "drop" in reason.lower()


def test_existing_row_count_reads_metadata(tmp_path):
    # 物理意图：用 pyarrow 元数据读行数，免全量读 454MB parquet（freshness 注释 ~1.75s）
    p = tmp_path / "lake.parquet"
    pd.DataFrame({"a": range(1234)}).to_parquet(p)
    assert existing_row_count(str(p)) == 1234


def test_existing_row_count_none_when_missing(tmp_path):
    # 首次写/新湖：文件不存在 → None（无历史可比，放行）
    assert existing_row_count(str(tmp_path / "nope.parquet")) is None


def test_existing_row_count_none_on_corrupt(tmp_path):
    # 损坏文件 → None（调用方据此判「基线不可读」，由 assert_safe_overwrite 决策）
    p = tmp_path / "bad.parquet"
    p.write_bytes(b"not a parquet")
    assert existing_row_count(str(p)) is None


# ============================================================================
# 写入守卫编排 assert_safe_overwrite（T13-A · Task 2）
# ============================================================================
# 物理意图：所有湖写入口（通用同步器全量覆盖/增量 append/repair 重写）落盘 to_parquet
# 前调本函数；决策矩阵：force 旁路 / 首次写放行 / 损坏拒写 / 空 df 拒写 / 骤降拒写。


def _write_lake(path, n):
    pd.DataFrame({"a": range(n)}).to_parquet(path)


def test_assert_safe_overwrite_raises_on_crater(tmp_path):
    # T12 核心断言：现有 10000 行，待写 100 行（骤降）→ 拒写抛异常
    p = tmp_path / "lake.parquet"
    _write_lake(str(p), 10000)
    tiny = pd.DataFrame({"a": range(100)})
    with pytest.raises(WriteGuardError):
        assert_safe_overwrite(str(p), tiny)
    # 拒写后原文件未被覆盖（行数不变）
    assert existing_row_count(str(p)) == 10000


def test_assert_safe_overwrite_passes_on_first_write(tmp_path):
    # 首次写/新湖：无现有文件 → 放行（无基线可比）
    p = tmp_path / "new.parquet"
    assert_safe_overwrite(str(p), pd.DataFrame({"a": range(50)}))  # 不抛


def test_assert_safe_overwrite_passes_on_growth(tmp_path):
    # 正常增量：现有 1000，待写 1200（增长）→ 放行
    p = tmp_path / "lake.parquet"
    _write_lake(str(p), 1000)
    assert_safe_overwrite(str(p), pd.DataFrame({"a": range(1200)}))


def test_assert_safe_overwrite_rejects_empty_new_df(tmp_path):
    # 空 df 落盘无意义且可能抹除 → 拒写
    p = tmp_path / "lake.parquet"
    _write_lake(str(p), 1000)
    with pytest.raises(WriteGuardError):
        assert_safe_overwrite(str(p), pd.DataFrame())


def test_assert_safe_overwrite_force_bypasses_but_logs(tmp_path, caplog):
    # 逃生口：force=True 旁路守卫（人为故意缩小重采），但仍 critical 留痕
    p = tmp_path / "lake.parquet"
    _write_lake(str(p), 10000)
    with caplog.at_level("CRITICAL", logger="data.integrity"):
        assert_safe_overwrite(str(p), pd.DataFrame({"a": range(100)}), force=True)
    assert any("FORCE" in r.message or "force" in r.message.lower() for r in caplog.records)


def test_assert_safe_overwrite_corrupt_existing_raises(tmp_path):
    # 现有文件损坏：基线不可读 → 拒写（宁拒不盲写），不静默放行
    p = tmp_path / "lake.parquet"
    p.write_bytes(b"not a parquet")
    with pytest.raises(WriteGuardError):
        assert_safe_overwrite(str(p), pd.DataFrame({"a": range(100)}))
