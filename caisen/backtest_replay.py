# -*- coding: utf-8 -*-
"""历史回放验证器（蔡森形态学流水线 Phase 2 · Task 10 · 上线 gate）。

物理定位（CLAUDE.md 极简 + 显式 + 无前视红线）：
    本模块是蔡森策略上线前的 gate——对每个交易日 T 用【T 及之前】数据滚动跑
    PatternScreener→TradePlanGenerator，模拟 T+1 回踩成交 + 止盈/止损/时间止损离场，
    统计胜率/平均盈亏比/最大回撤/命中数/形态分布/月度收益/平均持仓天数。

    核心职责（承 Task 9 rr 张力）：Task 9 review 发现 spec 默认 min_rr_ratio=3.0 与
    蔡森等额累加（breakout≈neckline 时 rr≈1.0）有张力——标准突破入场计划会被 rr≥3
    过滤。本任务用宽松 min_rr_ratio（如 1.5）收集样本，统计真实胜率/平均盈亏比，
    在报告里数据驱动建议生产 min_rr_ratio。这是先验 spec 与实证数据的校准点。

回放逻辑（无前视红线，严格 .loc[:T]）：
    对每个交易日 T（start..end）：
        close_T = price_data[symbol].loc[:T]      # 严格只用 T 及之前，无前视
        candidates = screener.screen({symbol: df.loc[:T]}, T)
        plans = plan.generate(candidates, cfg, risk, aum, T)
        对每个 plan：
            若 T+1 触及回踩区间（low≤entry_upper 且 high≥entry_lower）→ 模拟买入 entry_upper
            后续逐日（T+2..）：
                触 stop_loss       → 平（记亏，rr=-1.0）
                触 take_profit_2x  → 平（记盈，rr=+2.0）—— 第二波满足主止盈位
                触 take_profit     → 平（记盈，rr=+1.0）—— 第一波满足（简化：单笔全平）
                超 max_holding_bars 且浮盈 ≥ timeout_exit_threshold → 时间止损平（记实际 rr）
            记录该笔盈亏
    统计：win_rate / avg_rr / max_drawdown / n_hits / pattern_dist / 月度收益

无前视证明（红线断言）：
    screener 内部基于 causal_pivots（T 日只看 T-1 及之前 pivot，confirm_bars 隔离未来），
    plan.generate 基于 screener 输出（无新数据源），离场模拟逐日推进（只用已发生的 high/low）。
    红线断言：裁剪序列末段，前段回放结果一致（test_replay_no_lookahead 验证）。

防御性边界（CLAUDE.md 量化风控拷问）：
    - 流动性枯竭/极端行情：回踩成交假设 entry_upper 成交，实盘可能滑点；回放为保守估计，
      不模拟滑点（生产需额外 slippage 模型，Phase 3 ExecutionEngine 负责）；
    - 接口/状态机边界：单 plan 异常不中断整个回放（try/except 跳过，记录 debug）；
    - 部分成交：本回放简化为单笔全平（Phase 3 完整状态机处理分级止盈/部分成交）；
    - 停牌/缺失数据：T+1 数据缺失时跳过该 plan（无法判定回踩触发）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from caisen.config import StrategyConfig
from caisen.risk import RiskManager
from caisen.patterns.screener import PatternScreener
from caisen import plan as plan_mod


# 模块级 logger：单 plan 异常走 debug（不污染 prod 日志，但可调试追溯）
_logger = logging.getLogger(__name__)


@dataclass
class ReplayReport:
    """历史回放统计报告（蔡森策略上线 gate 的判定依据）。

    字段物理意图：
        n_hits：        命中（成交）交易笔数；
        win_rate：      胜率 = 盈利笔数 / n_hits（n_hits=0 时为 0.0）；
        avg_rr：        平均盈亏比 = sum(rr) / n_hits（每笔 rr 以 entry→exit 实际价差计算，
                        盈利 rr>0、止损 rr=-1.0、时间止损 rr=实际浮盈比）；
        max_drawdown：  最大回撤（基于累计 rr 曲线的 peak-to-trough 最大跌幅，负值）；
        pattern_dist：  形态分布 {"w_bottom": x, "head_shoulder": y}；
        monthly_returns：月度收益（按 entry_date 月份聚合的 rr 之和，{"2024-01": 2.5, ...}）；
        avg_holding_bars：平均持仓天数（exit_date - entry_date 的交易日数均值）；
        min_rr_ratio_recommendation：数据驱动的生产 min_rr_ratio 建议（基于胜率/平均盈亏比）；
        metadata：      补充元数据（完整 hits 列表、cfg 快照等，便于审计/无前视断言）。
    """
    n_hits: int
    win_rate: float
    avg_rr: float
    max_drawdown: float
    pattern_dist: dict
    monthly_returns: dict
    avg_holding_bars: float
    min_rr_ratio_recommendation: str
    metadata: dict = field(default_factory=dict, hash=False, compare=False)


def replay(
    price_data: dict,
    cfg: StrategyConfig,
    risk: RiskManager,
    start,
    end,
    aum: float,
    trading_calendar: Optional[pd.DatetimeIndex] = None,
) -> ReplayReport:
    """对 price_data 滚动执行 screen→plan→离场模拟，返回 ReplayReport。

    参数：
        price_data:    {symbol: DataFrame} 字典，每个 DataFrame 含 close/high/low/volume/amount，
                       index 为交易日（RangeIndex 或 DatetimeIndex）。
        cfg:           蔡森策略全参数模型（形态识别 + min_rr_ratio + 离场参数）。
        risk:          事前风控管理器（提供 macro_position_coef）。
        start / end:   回放起止交易日（index label，含两端）。
        aum:           账户总资金。
        trading_calendar: 可选交易日历（plan.generate 用）。

    返回：
        ReplayReport，含胜率/平均盈亏比/最大回撤/命中数/形态分布/月度收益/建议。

    无前视红线：
        每个 T 日的 screener.screen 只接收 price_data[symbol].loc[:T]（严格 T 及之前）。
        screener 内部 causal_pivots 已隔离未来函数（confirm_bars），plan.generate 基于其输出。
        离场模拟从 T+1 起逐日推进，只用已发生的 high/low/close，不前视。
    """
    screener = PatternScreener(cfg, risk)
    all_hits: list[dict] = []

    # —— 对每个交易日 T 滚动执行 screen → plan（严格 .loc[:T]）——
    for symbol, df in price_data.items():
        # T 的取值范围：[start, end]（index label）。
        # 用 df.index 定位 start/end 的位置，对每个 T 取 .loc[:T] 子序列喂 screener。
        for T in _iter_trading_days(df.index, start, end):
            try:
                # 严格无前视：只用 T 及之前的数据（含 T 当日）
                df_T = df.loc[:T]
                if len(df_T) < cfg.min_pattern_bars:
                    continue   # 数据不足以形成形态，跳过
                candidates = screener.screen({symbol: df_T}, T)
                if candidates.empty:
                    continue
                plans = plan_mod.generate(
                    candidates, cfg, risk, aum, T, trading_calendar,
                )
            except Exception as exc:
                # 单 T 日异常不中断回放（边界审查）
                _logger.debug("replay 跳过 symbol=%s T=%s 异常=%s",
                              symbol, T, type(exc).__name__)
                continue

            # —— 对每个 plan 模拟 T+1 回踩成交 + 后续离场 ——
            # 注：传 cfg.max_holding_bars 用于基于位置的持仓超时判定（RangeIndex 下
            # plan.max_holding_until 的 Timestamp 会失真，故用位置计数更稳健）。
            for p in plans:
                hit = _simulate_one_trade(df, p, T, cfg.max_holding_bars)
                if hit is not None:
                    all_hits.append(hit)

    # —— 汇总统计 ——
    stats = _compute_stats(all_hits)
    recommendation = _recommend_min_rr(stats)

    return ReplayReport(
        n_hits=stats["n_hits"],
        win_rate=stats["win_rate"],
        avg_rr=stats["avg_rr"],
        max_drawdown=stats["max_drawdown"],
        pattern_dist=stats["pattern_dist"],
        monthly_returns=stats["monthly_returns"],
        avg_holding_bars=stats["avg_holding_bars"],
        min_rr_ratio_recommendation=recommendation,
        metadata={"hits": all_hits, "cfg_min_rr_ratio": cfg.min_rr_ratio},
    )


# ---------------------------------------------------------------------------
# 离场模拟（简化版：单笔全平，不做分级止盈/部分成交——Phase 3 完整状态机负责）
# ---------------------------------------------------------------------------
def _simulate_one_trade(df: pd.DataFrame, p, entry_day, max_holding_bars: int) -> Optional[dict]:
    """对单个 TradePlan 模拟 T+1 回踩成交 + 后续止盈/止损/时间止损离场。

    参数：
        df:               完整价格 DataFrame（含 T 及之后所有日，用于推进离场模拟）。
        p:                TradePlan（含 entry/stop/take_profit/take_profit_2x）。
        entry_day:        形态形成日 T（index label）。
        max_holding_bars: 最大持仓周期（交易日数，从 entry_pos 起计）。

    返回：
        命中 dict（含 formed_at/entry_price/exit_price/exit_reason/rr/holding_bars），
        若 T+1 未触及回踩区间（未成交）则返回 None。

    离场逻辑（逐日推进，优先级：stop_loss > take_profit_2x > take_profit > timeout）：
        T+1：若 low≤entry_upper 且 high≥entry_lower → 成交（entry_price=entry_upper）；
             同日若触 stop/take_profit_2x 则当日离场（保守：先判 stop，防日内闪崩）；
        T+2..：逐日判 stop_loss（先）→ take_profit_2x → take_profit；
        超 max_holding_bars 且浮盈 < timeout_exit_threshold：时间止损砍亏（按当日 close 平）；
        若序列末尾仍未离场：still_open（按末根 close 记浮盈 rr）。

    注：max_holding 用位置计数（entry_pos + max_holding_bars），不依赖 plan.max_holding_until
    的 Timestamp——RangeIndex 下 Timestamp 会失真，位置计数对任意 index 类型都稳健。
    """
    idx = df.index
    # entry_day 在 index 中的位置
    try:
        entry_pos = idx.get_loc(entry_day)
    except KeyError:
        return None   # entry_day 不在 index（数据异常），跳过
    # T+1 位置
    if entry_pos + 1 >= len(idx):
        return None   # 无 T+1 数据，无法判定回踩触发
    next_pos = entry_pos + 1

    # —— T+1 回踩触发判定 ——
    # 物理意图：回踩挂单在 entry_lower..entry_upper 区间，T+1 的 low/high 触及该区间即成交。
    # 成交价取 entry_upper（保守：回踩挂单上限，实盘可能略低，回放为保守估计）。
    row_t1 = df.iloc[next_pos]
    high_t1 = float(row_t1["high"])
    low_t1 = float(row_t1["low"])
    # 触及条件：low ≤ entry_upper（价曾跌到挂单上限之下）且 high ≥ entry_lower（价曾在挂单下限之上）
    if not (low_t1 <= p.entry_upper and high_t1 >= p.entry_lower):
        return None   # T+1 未触及回踩区间，未成交
    entry_price = p.entry_upper

    # —— 后续逐日推进离场判定（T+1 当日也可离场，T+2..）——
    exit_price = None
    exit_reason = None
    exit_pos = None

    # 基于位置的 max_holding 超时点（entry_pos + max_holding_bars，稳健于任意 index 类型）
    max_hold_pos = entry_pos + max_holding_bars

    for pos in range(next_pos, len(idx)):
        row = df.iloc[pos]
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        # 优先级 1：stop_loss（日内触及止损 → 立即平，记亏）
        # 物理意图：止损是硬风控，优先于止盈（防日内闪崩穿止损后反弹的假象）。
        if low <= p.stop_loss:
            exit_price = p.stop_loss
            exit_reason = "stop_loss"
            exit_pos = pos
            break

        # 优先级 2：take_profit_2x（第二波满足主止盈位 → 平，记大盈）
        # 物理意图：第二波满足是主要止盈目标，先于第一波检查（更优离场）。
        if high >= p.take_profit_2x:
            exit_price = p.take_profit_2x
            exit_reason = "take_profit"
            exit_pos = pos
            break

        # 优先级 3：take_profit（第一波满足 → 平，记盈，简化单笔全平）
        if high >= p.take_profit:
            exit_price = p.take_profit
            exit_reason = "take_profit"
            exit_pos = pos
            break

        # 优先级 4：时间止损砍亏（持仓达 max_holding_bars 且浮盈 < timeout_exit_threshold → 离场）
        # 【B-3 修复】与实盘 check_exit（execution.py:142-148）完全对齐：百分比分母
        # (close-entry)/entry + profit<threshold→离场（砍亏）。
        # 旧实现用 R 分母 unrealized>=threshold→锁盈是错误的：与实盘运算符/分母/意图全反，
        # 且让超时浮亏单永不实现（继续持有到末尾记 still_open），系统性虚高回测胜率/盈亏比，
        # 可能放行实盘亏损策略通过上线 gate。现统一为「超时浮盈不足即砍亏」的行业惯例。
        if pos >= max_hold_pos:
            profit = (close - entry_price) / entry_price   # 百分比，与 check_exit 同口径
            if profit < p.timeout_exit_threshold:
                exit_price = close
                exit_reason = "timeout"
                exit_pos = pos
                break
            # 浮盈 ≥ threshold：未达砍亏条件，继续持有

    # —— 序列末尾仍未离场 → still_open（按末根 close 记浮盈）——
    if exit_price is None:
        last_row = df.iloc[-1]
        exit_price = float(last_row["close"])
        exit_reason = "still_open"
        exit_pos = len(idx) - 1

    # —— 计算该笔 rr（以 entry→exit 实际价差 / 单笔风险(entry-stop)）——
    risk_per_unit = entry_price - p.stop_loss
    if risk_per_unit <= 0:
        return None   # 防御：entry≤stop 无意义，跳过
    rr = (exit_price - entry_price) / risk_per_unit

    return {
        "symbol": p.symbol,
        "pattern_type": p.pattern_type,
        "formed_at": p.formed_at,
        "entry_date": idx[entry_pos],
        "entry_day": entry_day,
        "entry_price": entry_price,
        "entry_upper": p.entry_upper,
        "entry_lower": p.entry_lower,
        "stop_loss": p.stop_loss,
        "take_profit": p.take_profit,
        "take_profit_2x": p.take_profit_2x,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "exit_date": idx[exit_pos],
        "exit_day": idx[exit_pos],
        "rr": rr,
        "holding_bars": exit_pos - entry_pos,
    }


# ---------------------------------------------------------------------------
# 统计计算（纯函数，便于单元测试直接验证）
# ---------------------------------------------------------------------------
def _compute_stats(hits: list[dict]) -> dict:
    """从命中交易列表计算胜率/平均盈亏比/最大回撤/形态分布/月度收益/平均持仓天数。

    参数：
        hits: _simulate_one_trade 输出的命中 dict 列表。

    返回：
        dict 含 n_hits/win_rate/avg_rr/max_drawdown/pattern_dist/monthly_returns/avg_holding_bars。

    统计定义：
        - win_rate = 盈利笔数(rr>0) / n_hits；
        - avg_rr = sum(rr) / n_hits；
        - max_drawdown：基于累计 rr 曲线的 peak-to-trough 最大跌幅（负值，0 表示无回撤）；
        - pattern_dist：按 pattern_type 计数；
        - monthly_returns：按 entry_date 月份聚合 rr 之和；
        - avg_holding_bars：exit_pos - entry_pos 的均值（交易日数）。

    防御性：hits 为空时所有统计归零（不除零，不抛异常）。
    """
    if not hits:
        return {
            "n_hits": 0,
            "win_rate": 0.0,
            "avg_rr": 0.0,
            "max_drawdown": 0.0,
            "pattern_dist": {},
            "monthly_returns": {},
            "avg_holding_bars": 0.0,
        }

    n = len(hits)
    rrs = [float(h["rr"]) for h in hits]
    wins = sum(1 for r in rrs if r > 0)

    # 累计 rr 曲线 → 最大回撤（peak-to-trough）
    # 物理意图：回撤 = 历史峰值到后续谷值的最大跌幅，反映策略最坏阶段性回撤。
    cumulative = []
    running = 0.0
    for r in rrs:
        running += r
        cumulative.append(running)
    peak = float("-inf")
    max_dd = 0.0
    for v in cumulative:
        peak = max(peak, v)
        dd = v - peak   # dd ≤ 0（谷值低于峰值时为负）
        if dd < max_dd:
            max_dd = dd   # 保留最负值（最大回撤）

    # 形态分布
    pattern_dist: dict = {}
    for h in hits:
        pt = h.get("pattern_type", "unknown")
        pattern_dist[pt] = pattern_dist.get(pt, 0) + 1

    # 月度收益：按 entry_date 月份聚合 rr（entry_date 可能是 int 或 Timestamp）
    monthly_returns: dict = {}
    for h in hits:
        ed = h.get("entry_date")
        if ed is None:
            continue
        try:
            ts = pd.Timestamp(ed)
            key = f"{ts.year}-{ts.month:02d}"
        except Exception:
            continue   # 非法日期跳过（不污染聚合）
        monthly_returns[key] = monthly_returns.get(key, 0.0) + float(h["rr"])

    # 平均持仓天数
    avg_holding = sum(float(h.get("holding_bars", 0)) for h in hits) / n

    return {
        "n_hits": n,
        "win_rate": wins / n,
        "avg_rr": sum(rrs) / n,
        "max_drawdown": max_dd,
        "pattern_dist": pattern_dist,
        "monthly_returns": monthly_returns,
        "avg_holding_bars": avg_holding,
    }


def _recommend_min_rr(stats: dict) -> str:
    """基于回放统计（胜率/平均盈亏比）数据驱动建议生产 min_rr_ratio。

    决策逻辑（数据驱动校准 Task 9 spec 默认 3.0）：
        - 样本不足（n_hits < 5）：建议先用宽松阈值（1.5）积累样本，暂不定论；
        - 期望值 EV = 胜率 × 平均盈亏比 - (1 - 胜率) × 1.0（止损亏 1R）；
          EV > 0.2 → 当前阈值有效（保留高质量过滤）；
          EV ∈ [0, 0.2] → 边际有效，建议适度放宽以增加样本；
          EV < 0 → 阈值过严或策略无效，建议更宽松阈值重新评估或回炉优化。
    返回中文建议字符串（含具体数值依据）。
    """
    n = stats["n_hits"]
    wr = stats["win_rate"]
    avg_rr = stats["avg_rr"]

    if n < 5:
        return (
            f"样本不足（n_hits={n} < 5）：建议先用宽松 min_rr_ratio=1.5 积累更多样本，"
            f"待样本量充足后再数据驱动定标。承 Task 9 rr 张力：标准突破入场计划 rr≈1.0，"
            f"生产默认 3.0 会过滤绝大部分样本。"
        )

    # EV 计算：盈利笔贡献 wr×avg_rr，亏损笔贡献 (1-wr)×(-1.0)
    ev = wr * avg_rr - (1 - wr) * 1.0
    if ev > 0.2:
        return (
            f"建议保留当前阈值（EV={ev:.3f} > 0.2，胜率={wr:.1%}，平均盈亏比={avg_rr:.2f}）。"
            f"数据显示当前 min_rr_ratio 过滤后的样本具有正期望，高质量过滤有效。"
        )
    if ev >= 0:
        return (
            f"建议适度放宽阈值（EV={ev:.3f} ∈ [0, 0.2]，胜率={wr:.1%}，"
            f"平均盈亏比={avg_rr:.2f}）。边际有效但样本偏少，可适度下调 min_rr_ratio "
            f"（如从 3.0 降至 2.0）以增加样本量与总期望。"
        )
    return (
        f"建议更宽松阈值重新评估或回炉优化策略（EV={ev:.3f} < 0，胜率={wr:.1%}，"
        f"平均盈亏比={avg_rr:.2f}）。当前样本期望为负，min_rr_ratio 过严或策略形态"
        f"识别需优化；建议下调至 1.5 重新回放验证。"
    )


# ---------------------------------------------------------------------------
# 辅助：交易日迭代（支持 RangeIndex 与 DatetimeIndex）
# ---------------------------------------------------------------------------
def _iter_trading_days(index, start, end):
    """生成 [start, end] 闭区间的交易日 index label 序列（含两端）。

    兼容 RangeIndex（整数 index）与 DatetimeIndex（日期 index）：
        - 用 searchsorted 定位 start/end 在 index 中的位置；
        - 切片 [start_pos, end_pos] 迭代。

    防御性：start/end 越界时取 index 的首/末兜底（不抛异常）。
    """
    n = len(index)
    if n == 0:
        return []
    # searchsorted side="left" 返回 ≥ target 的插入位置
    start_pos = index.searchsorted(start, side="left") if not isinstance(index, pd.RangeIndex) \
        else max(int(start) - int(index[0]), 0)
    end_pos = index.searchsorted(end, side="right") if not isinstance(index, pd.RangeIndex) \
        else min(int(end) - int(index[0]) + 1, n)

    # 兜底：越界取首/末
    start_pos = max(0, min(start_pos, n))
    end_pos = max(0, min(end_pos, n))
    if start_pos >= end_pos:
        return []
    return list(index[start_pos:end_pos])
