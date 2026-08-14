# -*- coding: utf-8 -*-
"""A1 市场状态闸（DG-G4 定稿 · 2026-08-14）：沪深300 200 日均线 + 市场宽度双确认。

物理意图：颈线法是突破策略，wf 四折实证其冠军参数在 2022 熊市折外 calmar=-0.62
（熊市负期望坐实）——空头环境假突破多，正确动作是停手不进场。本模块给执行侧
（engine._eod 选股前置 + _pre_open_gate ④ 段）提供单源的 regime 判定。

判据（DG-G4 红线：阈值固定经验值，绝不进 TPE/PARAM_SPACE——否则搜索会过拟合
regime 参数本身，重蹈 2025 特化覆辙）：
    BULL    = 沪深300 收盘 > MA200  ∧  宽度 > 0.5        → 允许新单
    BEAR    = 任一不满足                              → 停手（只断新单，存量退出照常）
    UNKNOWN = 数据缺失（指数 <200 根 / 宽度样本 <500 只 / 读湖异常）
              → 调用方 fail-closed 视同 BEAR（DG-G3 哲学：缺信息时收紧而非放开）

宽度 = a_shares_daily 全市场【末位有效 K 线】close > 各自 MA200 的标的占比
（时点宽度，非区间宽度——"今天有多少标的站在年线上方"；防「指数靠权重股撑、
市场实际已转空」的假多头）。

当日缓存：宽度计算要读 455MB 主湖，一天只算一次（eod 一次 + pre_open 复用）；
盘中新数据不改当日判定（regime 是环境闸非交易信号，日内翻转由次日判定吸收）。
"""
from __future__ import annotations

from dataclasses import dataclass

# DG-G4 定稿常量（红线：绝不进 TPE；改值须走 ADR）
MA_WINDOW = 200            # 200 日均线（牛熊分界主流口径）
BREADTH_THRESHOLD = 0.5    # 宽度过半 = 市场多数标的在年线上方
BREADTH_MIN_STOCKS = 500   # 宽度统计最小样本（防残缺湖误判「假宽度」）
HS300 = "000300.SH"

_BREADTH_TAIL_DAYS = 260   # 宽度计算只取近 260 交易日（MA200 需 200 根 + 余量，
                           # 全历史 groupby rolling 纯浪费——A2 湖窗已 1030 万行）


@dataclass(frozen=True)
class RegimeState:
    """regime 判定结果（不可变值对象）。state ∈ {BULL, BEAR, UNKNOWN}。"""
    state: str
    reason: str   # 人读中文（含数据细节，供播报/日志定位）
    asof: str     # 判定所据最后交易日 ISO（观测面）


# 当日缓存（进程内）：{基准日键: RegimeState}——同日多触发点共享一次读湖。
# 回放模式（asof 各异）键互异不互撞；生产 latest 键一日一算。
_CACHE: dict[str, RegimeState] = {}


def classify(index_df=None, daily_df=None, asof=None) -> RegimeState:
    """三态 regime 判定。df 参数注入供测试；None 时读 data_lake 两湖。

    Args:
        index_df: 指数日线（index=[date, symbol]，close 列；None 读 index_daily 湖）。
        daily_df: 全市场日线（index=[date, symbol]，close 列；None 读 a_shares_daily 湖）。
        asof: 显式判定基准日（回放/测试用）；None = 湖数据最新日（生产路径）。
    读湖/计算任何异常 → UNKNOWN（fail-closed 语义由调用方执行停手，本模块不放行）。
    """
    key = str(asof) if asof is not None else "latest"
    if key in _CACHE:
        return _CACHE[key]
    try:
        if index_df is None or daily_df is None:
            index_df, daily_df = _load_lake()
        st = _classify_sync(index_df, daily_df, asof)
    except Exception as exc:   # 读湖/对齐任何异常 → UNKNOWN 不放行（fail-closed）
        st = RegimeState("UNKNOWN", f"regime 判定异常（fail-closed）：{exc!r}", key)
    _CACHE[key] = st
    return st


def _load_lake():
    """读两湖（index_daily / a_shares_daily）。延迟 import：模块加载零 pandas 成本。"""
    import pandas as pd
    idx = pd.read_parquet("data_lake/index_daily.parquet")
    daily = pd.read_parquet("data_lake/a_shares_daily.parquet")
    return idx, daily


def _classify_sync(index_df, daily_df, asof) -> RegimeState:
    """纯同步判定（classify 的核心，测试经 df 注入直达）。"""
    import pandas as pd
    # ── ① 指数腿：HS300 末位收盘 vs MA200 ──────────────────────────────
    try:
        hs = index_df.xs(HS300, level="symbol").sort_index()
    except KeyError:
        return RegimeState("UNKNOWN", f"指数湖无 {HS300}", str(asof or "?"))
    if asof is not None:
        hs = hs[hs.index <= pd.Timestamp(asof)]
    if len(hs) < MA_WINDOW + 1:
        return RegimeState(
            "UNKNOWN", f"指数历史不足 {MA_WINDOW + 1} 根（现 {len(hs)}）",
            str(hs.index[-1].date()) if len(hs) else str(asof or "?"))
    ma = hs["close"].rolling(MA_WINDOW, min_periods=MA_WINDOW).mean()
    last_date = hs.index[-1]
    last_close = float(hs["close"].iloc[-1])
    last_ma = float(ma.iloc[-1])
    import math
    if math.isnan(last_ma):
        return RegimeState("UNKNOWN", "MA200 为 NaN", str(last_date.date()))
    price_ok = last_close > last_ma

    # ── ② 宽度腿：全市场【末位行】close>各自 MA200 占比（时点宽度）────────
    dates = daily_df.index.get_level_values("date").unique().sort_values()
    tail_dates = dates[-_BREADTH_TAIL_DAYS:]
    tail = daily_df.loc[tail_dates[0]:] if len(dates) > _BREADTH_TAIL_DAYS else daily_df
    if asof is not None:
        tail = tail[tail.index.get_level_values("date") <= pd.Timestamp(asof)]
    # 每标的滚动 MA200（groupby transform，仅 260 日窗口——控计算量）
    ma_s = (tail["close"].groupby(level="symbol")
            .transform(lambda s: s.rolling(MA_WINDOW, min_periods=MA_WINDOW).mean()))
    with_ma = tail.assign(_ma=ma_s)
    # 末位行：每标的窗口内最后一根 K 线（时点宽度的「时点」）
    last_rows = with_ma.groupby(level="symbol").tail(1)
    last_rows = last_rows[last_rows["close"].notna() & last_rows["_ma"].notna()]
    n_valid = len(last_rows)
    if n_valid < BREADTH_MIN_STOCKS:
        # 样本残缺：指数腿好也 UNKNOWN（缺信息收紧）；指数腿差直接 BEAR（已有定论）
        return RegimeState(
            "BEAR" if not price_ok else "UNKNOWN",
            f"宽度样本不足 {BREADTH_MIN_STOCKS} 只（现 {n_valid}）",
            str(last_date.date()))
    breadth = float((last_rows["close"] > last_rows["_ma"]).mean())
    breadth_ok = breadth > BREADTH_THRESHOLD

    # ── 双确认合成 ────────────────────────────────────────────────────
    if price_ok and breadth_ok:
        return RegimeState(
            "BULL", f"HS300 {last_close:.0f}>MA200 {last_ma:.0f}，宽度 {breadth:.0%}",
            str(last_date.date()))
    why = []
    if not price_ok:
        why.append(f"HS300 {last_close:.0f}≤MA200 {last_ma:.0f}")
    if not breadth_ok:
        why.append(f"宽度 {breadth:.0%}≤{BREADTH_THRESHOLD:.0%}")
    return RegimeState("BEAR", "；".join(why), str(last_date.date()))
