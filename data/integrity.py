# -*- coding: utf-8 -*-
"""数据完整性核心：停牌区间重建 + 交易日基准（阶段 1 基础组件）。

物理意图：lake 数据有完整性缺口——标的停牌复牌段漏采，导致颈线法等识别用残缺数据
误判（300214.SZ 案例：缺 07-14~07-21 → 颈线 8.07 → 07-24 close 11.86 误判突破）。
本模块提供「区分合法跳空（停牌）vs 漏采」的基础设施，供规则 1/3/4 复用。

停牌语义（A 股物理事实，算法必须遵守）：
- S（停牌）当日：标的停牌，无行情 → 算停牌日（lake 缺这根 = 合法跳空）
- R（复牌）当日：标的恢复交易，有行情 → 不算停牌日（lake 应有这根，缺 = 漏采）
- 停牌区间 = [S 日, R 日) 之间的交易日（含 S 不含 R）
- 未复牌（S 后无 R）：从 S 到最新交易日都算停牌

依赖注入红线：load_suspend_intervals 接收 suspend_df + trade_days_set 作入参（纯函数，
不读文件/不触网），便于单测；文件读取与 token 获取由调用方（find_gaps / scan CLI）负责。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Set

import pandas as pd


# ============================================================================
# 结果数据结构（frozen，仿 data/freshness.py 的 FreshnessResult，便于聚合与断言）
# ============================================================================

@dataclass(frozen=True)
class GapRange:
    """一段缺口（规则 1 扫描输出 / 规则 3 补采输入）。

    suspend_justified=True 表示整段是停牌（合法跳空，无需补采）；False 表示漏采（需补）。
    """
    symbol: str
    start: str                            # 缺口起 YYYY-MM-DD
    end: str                              # 缺口止 YYYY-MM-DD
    missing_dates: tuple[str, ...]        # 该段缺失的交易日（不可变，可哈希）
    suspend_justified: bool


@dataclass(frozen=True)
class ContinuityResult:
    """窗口连续性检查结果（规则 4 scan_live gate 用）。

    ok=True 表示窗口完整或仅含停牌跳空；False 表示含未解释漏采（gate 据此跳过标的）。
    """
    ok: bool
    missing_dates: tuple[str, ...]        # 窗口内所有缺失交易日
    unjustified: tuple[str, ...]          # 缺失且非停牌 = 漏采（gate 判定依据）


# ============================================================================
# 规则 2：suspend_d S/R 事件 → per-symbol 停牌交易日集合
# ============================================================================

def load_suspend_intervals(
    suspend_df: pd.DataFrame, trade_days_set: Set[str]
) -> dict[str, Set[str]]:
    """suspend_d 的 S/R 事件 → per-symbol 停牌交易日集合。

    Args:
        suspend_df: suspend_d parquet 内容（MultiIndex(date, symbol) + suspend_type 列，
                    suspend_type ∈ {"S","R"}，suspend_timing 字段不可靠忽略）。
        trade_days_set: 全期交易日集合（YYYY-MM-DD），用于把 S/R 区间展开为交易日
                        （S/R 之间的非交易日不算停牌——本就无行情）。

    Returns:
        {symbol: set(停牌交易日 YYYY-MM-DD)}。无停牌记录的标的不在 dict
        （调用方 .get(sym, set()) 取空集，语义=该标的从未停牌）。
    """
    intervals: dict[str, Set[str]] = {}
    # per-symbol 独立重建（groupby 保证不跨标的串味）
    for sym, grp in suspend_df.groupby(level="symbol"):
        events = (grp.reset_index()
                  .sort_values("date")[["date", "suspend_type"]])
        sym_days: Set[str] = set()
        pending_S: str | None = None
        # 逐事件扫描：S 起区间、R 终区间
        for _, row in events.iterrows():
            dstr = pd.Timestamp(row["date"]).strftime("%Y-%m-%d")
            t = row["suspend_type"]
            if t == "S":
                pending_S = dstr
                sym_days.add(dstr)            # S 当日停牌（无行情）
            elif t == "R":
                if pending_S is not None:
                    # [pending_S, R) 之间的交易日加入（含 S 已加，补中间交易日；R 不含=复牌有行情）
                    for td in trade_days_set:
                        if pending_S <= td < dstr:
                            sym_days.add(td)
                pending_S = None
        # 末尾未复牌（S 后无 R）：从 S 到最大交易日都算停牌
        if pending_S is not None:
            for td in trade_days_set:
                if td >= pending_S:
                    sym_days.add(td)
        if sym_days:
            intervals[sym] = sym_days
    return intervals


# ============================================================================
# 交易日基准：trade_cal → 区间交易日集合
# ============================================================================

def fetch_trade_days(start: str, end: str) -> Set[str]:
    """[start, end] 闭区间的交易日集合（YYYY-MM-DD）。

    逐年 _fetch_year_trade_cal 合并（跨年区间需多年），过滤到 [start, end]。
    边界含 start/end（与 DataLakeReader.get_timeseries 的 .loc[start:end] 闭区间同口径）。
    """
    sy, ey = int(start[:4]), int(end[:4])
    days: Set[str] = set()
    for y in range(sy, ey + 1):
        for d in _fetch_year_trade_cal(y):
            if start <= d <= end:
                days.add(d)
    return days


def _fetch_year_trade_cal(year: int) -> list[str]:
    """封装 trading/calendar.fetch_trade_cal（便于测试 monkeypatch，避免触网）。

    返回该年交易日列表（YYYY-MM-DD）。无 token/网络时 calendar 内部 weekday 兜底
    （仅识周末不识节假日——上层应据告警排查）。
    """
    from trading.calendar import fetch_trade_cal
    return fetch_trade_cal(year)


# ============================================================================
# 规则 1：全市场连续性扫描
# ============================================================================

def find_gaps(
    df_lake: pd.DataFrame, trade_days_set: Set[str],
    suspend_intervals: dict[str, Set[str]],
) -> list[GapRange]:
    """全市场连续性扫描：找出每个标的在 [首日, 末日] 区间内「应有却缺失」的交易日段。

    算法（实测全市场 ~1.2s，groupby level=symbol 一次扫）：
        per-symbol: expected = trade_days ∩ [actual_min, actual_max]
                    沿 trade_days 顺序遍历，连续的 missing（非 actual）合并成一段
    区间边界用 [actual_min, actual_max]：标的上市前/退市后的日期不要求（避免误报）。

    Args:
        df_lake: daily 湖（MultiIndex(date, symbol) + OHLCV 列）。
        trade_days_set: 全期交易日集合（fetch_trade_days 输出）。
        suspend_intervals: load_suspend_intervals 输出（{symbol: 停牌日集合}）。

    Returns:
        list[GapRange]：每个 GapRange 是一段连续缺口；整段全停牌 → suspend_justified=True
        （合法跳空），否则 False（含漏采，需补）。
    """
    gaps: list[GapRange] = []
    # groupby(sort=False) 不额外排序（lake 已 sort_index），groupby 键即唯一 symbol。
    for sym, grp in df_lake.groupby(level="symbol", sort=False):
        dates_idx = grp.index.get_level_values("date")
        actual = {pd.Timestamp(d).strftime("%Y-%m-%d") for d in dates_idx}
        if not actual:
            continue
        amin, amax = min(actual), max(actual)
        # expected = 该标的首末交易日之间的所有交易日（sorted 保证遍历顺序 = 时间顺序）
        expected_sorted = sorted(d for d in trade_days_set if amin <= d <= amax)
        seg: list[str] = []
        for d in expected_sorted:
            if d in actual:
                if seg:  # 遇到 actual → 连续 missing 段结束
                    gaps.append(_build_gap_range(sym, seg, suspend_intervals))
                    seg = []
            else:
                seg.append(d)
        if seg:  # 末尾缺口段
            gaps.append(_build_gap_range(sym, seg, suspend_intervals))
    return gaps


def _build_gap_range(
    symbol: str, seg: list[str], suspend_intervals: dict[str, Set[str]]
) -> GapRange:
    """把连续 missing 段组装成 GapRange，整段全停牌则 suspend_justified=True。"""
    susp = suspend_intervals.get(symbol, set())
    justified = all(d in susp for d in seg)
    return GapRange(
        symbol=symbol, start=seg[0], end=seg[-1],
        missing_dates=tuple(seg), suspend_justified=justified,
    )


# ============================================================================
# 规则 4：窗口连续性检查（scan_live gate 用）
# ============================================================================

def check_window_continuity(
    df_window: pd.DataFrame, trade_days_set: Set[str],
    suspend_intervals: dict[str, Set[str]], symbol: str,
) -> ContinuityResult:
    """窗口连续性检查：判断 df_window（单标的 OHLCV）是否含未解释漏采。

    gate 判定逻辑：
        expected = trade_days ∩ [窗口首日, 窗口末日]
        missing = expected - 窗口实际索引
        unjustified = missing - suspend_intervals[symbol]   （漏采 = 非停牌的缺失）
        ok = (unjustified 为空)

    ok=True 表示窗口完整或仅含停牌跳空（合法）；ok=False 表示含漏采，gate 应跳过该标的。

    Args:
        df_window: 单标的 OHLCV（DatetimeIndex），通常是 df_upto.tail(window)。
        trade_days_set: 全期交易日集合。
        suspend_intervals: load_suspend_intervals 输出。
        symbol: 当前标的（查停牌区间用）。
    """
    actual = {pd.Timestamp(d).strftime("%Y-%m-%d") for d in df_window.index}
    if not actual:
        # 空窗口不拦（detect 内部 len(df)<window 检查会处理短窗口）
        return ContinuityResult(ok=True, missing_dates=(), unjustified=())
    wmin, wmax = min(actual), max(actual)
    expected = {d for d in trade_days_set if wmin <= d <= wmax}
    missing = sorted(expected - actual)
    if not missing:
        return ContinuityResult(ok=True, missing_dates=(), unjustified=())
    susp = suspend_intervals.get(symbol, set())
    unjustified = tuple(d for d in missing if d not in susp)
    return ContinuityResult(
        ok=(len(unjustified) == 0),
        missing_dates=tuple(missing),
        unjustified=unjustified,
    )
