# -*- coding: utf-8 -*-
"""历史回放验证器（策略中立版 · 2026-07-20 与策略解耦重构）。

物理定位（解耦后）：
    回测引擎只做"逐 symbol×T 滚动调度 + 跨 symbol 聚合统计 + ReplayReport 组装"，
    不依赖任何具体策略。策略（caisen 形态/颈线法）实现 strategies.base.Strategy，
    经 scan_at(symbol, df_T, T, state) 一站式产出 trade dict 列表，引擎汇入统计。

    出场逻辑归策略侧：颈线法 simulate_exit 是完整状态机（挂单回踩+撤单+分级止盈），
    经 decide_exit 单根 K 线判定（颈线法专属 NecklineExitDecision 契约）；caisen 形态
    _simulate_one_trade（T+1回踩+移动止盈）已随形态代码退役删除——出场判定现单源于
    strategies/neckline/execution.py，引擎零感知。

解耦前（已搬走）：原 screener/plan/zigzag/_simulate_one_trade 形态专属逻辑迁至
    strategies/caisen_pattern.py（CaisenPatternStrategy 适配器，阶段E 随形态代码删）。

无前视红线（不变）：引擎传给 scan_at 的 df_T 严格 = df.loc[:T]；策略内部用预算全序列
    指标的 .iloc[:T_pos+1] 截断，不读 T 之后数据。

防御性边界（CLAUDE.md 量化风控拷问）：
    - 单 T 异常不中断回放（try/except 跳过，记 debug）；
    - abort_cb 命中抛 ReplayAborted（区分用户取消 vs 异常崩溃）；
    - 策略 scan_at 返回空列表（未触发/未成交）→ 引擎自然跳过。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from backtest.models import PositionModel, build_equity_curve
from strategies.base import Strategy


# 模块级 logger：单 T 异常走 debug（不污染 prod 日志，但可调试追溯）
_logger = logging.getLogger(__name__)


@dataclass
class ReplayReport:
    """历史回放统计报告（策略中立——字段对齐通用统计，pattern_dist 阶段D 泛化为 signal_dist）。

    字段物理意图：
        n_hits：        命中（成交）交易笔数；
        win_rate：      胜率 = 盈利笔数 / n_hits（n_hits=0 时为 0.0）；
        avg_rr：        平均盈亏比 = sum(rr) / n_hits（rr 为风险倍数）；
        max_drawdown：  最大回撤（基于累计 rr 曲线 peak-to-trough，负值）；
        pattern_dist：  信号类型分布（阶段D 改名 signal_dist；颈线法 {"neckline":x}，caisen 形态分布）；
        monthly_returns：月度收益（按 entry_date 月份聚合 rr）；
        avg_holding_bars：平均持仓天数；
        threshold_recommendation：阈值建议（阶段D 改名；旧名 min_rr_ratio_recommendation
            保留为属性别名向后兼容）；
        equity_curve：  资金曲线（PositionModel 模型，默认 pos_cap=5% 单笔仓位加总 +
            最大并发持仓 6；旧 RISK_FRAC=0.01 复利口径经 PositionModel(risk_frac=0.01)
            显式启用），归一化 equity_0=1.0；
        trades：        逐笔买卖流水；
        annualized_return：年化 CAGR；
        n_trading_days：区间交易日数；
        metadata：      补充（hits 列表、cfg 阈值、策略名，便于审计）。
    """
    n_hits: int
    win_rate: float
    avg_rr: float
    max_drawdown: float
    pattern_dist: dict
    monthly_returns: dict
    avg_holding_bars: float
    threshold_recommendation: str
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    annualized_return: float = 0.0
    n_trading_days: int = 0
    metadata: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def min_rr_ratio_recommendation(self) -> str:
        """旧字段名别名（caisen 时代归档/调用方向后兼容；新代码用新名）。"""
        return self.threshold_recommendation


class ReplayAborted(Exception):
    """回测被用户取消（abort_cb 于 symbol 循环顶返回 True 时抛出）。

    物理意图：异步回测的取消信号——调度器置 abort flag，worker 的 abort_cb 读 flag，
    replay 在每个 symbol 处理前检查，命中即抛本异常向上冒泡。worker 捕获后标 task
    CANCELLED（非 FAILED——区分「用户主动取消」与「异常崩溃」）。
    """


def _apply_continuity_filter(price_data: dict, strategy, start, end) -> dict:
    """Task 7 U5：replay 前过滤窗口含未解释漏采的 symbol（回测/实盘同源 filter）。

    物理意图（300214.SZ 漏采教训 · memory data-lake-integrity-gap）：
        完整性 gate 已从 scan_live 上提到 data/integrity.filter_universe_by_continuity
        （universe 级 pre-filter）。replay 在滚动 scan_at 前先 filter price_data——
        与 trading/engine._eod 共用同一 filter（数据校验单源）。

    策略 window 解析：从 strategy.id_cfg["window"] 取（颈线法），缺失兜底 60。
    susp/trade_days 加载：fetch_trade_days(start, end) + 读 data_lake/suspend_d.parquet。
    fail-open（与 _eod._load_integrity_ctx 同口径）：加载失败返 ({}, set()) → filter 全放行
    （trade_days 空集 → check_window_continuity 的 expected 恒空 → ok=True）。

    Returns:
        过滤后的 price_data（{symbol: df}，仅含通过完整性 gate 的 symbol）。
    """
    try:
        from data.integrity import filter_universe_by_continuity, load_suspend_intervals, fetch_trade_days
        import pandas as _pd
        from pathlib import Path as _Path
        # 解析策略 window（与 _eod._resolve_id_window 同源，缺失兜底 60）
        try:
            window = int(getattr(strategy, "id_cfg", {}).get("window") or 60)
        except (TypeError, ValueError):
            window = 60
        # 加载 trade_days + susp（fail-open）
        try:
            trade_days = fetch_trade_days(str(start), str(end))
            susp_path = _Path("data_lake/suspend_d.parquet")
            if susp_path.exists():
                susp_df = _pd.read_parquet(susp_path)
                susp = load_suspend_intervals(susp_df, trade_days)
            else:
                susp = {}
        except Exception as _e:
            _logger.warning("replay 完整性 gate 上下文加载失败（fail-open 放行）：%s", _e)
            trade_days, susp = set(), {}
        # filter price_data（universe=price_data.keys()，df_map=price_data）
        clean_syms = filter_universe_by_continuity(
            list(price_data.keys()), price_data, window, susp, trade_days)
        return {sym: price_data[sym] for sym in clean_syms}
    except Exception as _e:
        # filter 自身异常 fail-open（不阻断回测）——退回原 price_data 全放行
        _logger.warning("replay filter_universe_by_continuity 异常（fail-open 放行）：%s", _e)
        return price_data


def replay(
    price_data: dict,
    strategy: Strategy,
    start,
    end,
    *,
    progress_cb=None,           # Callable[[done:int, total:int], None] —— 每 50 symbol 上报一次
    abort_cb=None,              # Callable[[], bool] —— True 即中止（symbol 循环顶抛 ReplayAborted）
    position_model: Optional[PositionModel] = None,   # 组合资金模型（默认 pos_cap 口径）
) -> ReplayReport:
    """对 price_data 滚动调 strategy.scan_at，返回 ReplayReport（策略中立）。

    参数：
        price_data: {symbol: DataFrame} 字典，每个含 close/high/low/volume/amount，index 为交易日。
        strategy:   实现 strategies.base.Strategy 的策略实例（caisen 形态 / 颈线法）。
        start/end:  回放起止交易日（index label，含两端）。

    引擎职责：逐 symbol×T 滚动（无前视 .loc[:T]）+ abort/progress 调度 + 跨 symbol 聚合统计。
    策略职责：precompute（指标预算）+ scan_at（识别+进场+出场一站式，返回 trade dict 列表）。

    ⚠️ 实例契约（2026-08-02 真实回放验证发现）：strategy 实例带跨 T 状态
    （scan_at 写 strategy_state / 颈线法 cooldown 锚点），**一个实例只能跑一次回放**——
    复用同一实例跑第二次会静默污染结果（cooldown 残留 → 命中被吞）。调用方必须
    每个任务新建策略实例（worker / compute_unit.evaluate_replay 已遵循此契约）。

    无前视红线：传给 scan_at 的 df_T 严格 = df.loc[:T]；策略内部预算指标用 .iloc[:T_pos+1] 截断。

    Task 7 U5 gate 下沉（2026-07-29 · 300214.SZ 漏采教训）：
        replay 前调 ``filter_universe_by_continuity`` 过滤窗口含未解释漏采的 symbol——
        与 trading/engine._eod 共用同一 filter（回测/实盘数据校验单源）。策略层 scan_at
        假设 df 已通过完整性 gate（颈线法 scan_live 已删内联 gate，回测经此 filter 兜底）。
        ⚠️ 本 filter 只检 price_data 末端 window 日（df.tail(window)），与 _eod 实盘窗口同口径
        （实盘 T 日盘后窗口就在末端）；回测中段窗口的漏采由 scan_at 逐 T 的 df_T.tail(window)
        隐式覆盖（df_T 是 .loc[:T] 截断，中段漏采会持续命中末端窗口直到滚出）。
        fail-open：susp/trade_days 加载失败 → trade_days 空集 → filter 全放行（不阻断回测）。
    """
    # ── Task 7 U5：完整性 gate pre-filter（回测/实盘同源 data.integrity.filter）──
    price_data = _apply_continuity_filter(price_data, strategy, start, end)

    # —— 预算：每 symbol 调一次 strategy.precompute（ATR/HV/pivots 等下沉策略）——
    state = {sym: strategy.precompute(sym, df) for sym, df in price_data.items()}

    all_hits: list = []
    total_symbols = len(price_data)
    _done = 0
    _PROGRESS_EVERY = 50        # 全市场 5000 只 ≈ 100 次上报
    for symbol, df in price_data.items():
        # 取消检查点（symbol 循环顶）：abort_cb 命中即抛 ReplayAborted → task 标 CANCELLED。
        if abort_cb is not None and abort_cb():
            raise ReplayAborted()
        sym_state = state[symbol]
        for T in _iter_trading_days(df.index, start, end):
            try:
                df_T = df.loc[:T]   # 严格无前视：只用 T 及之前的数据（含 T 当日）
                hits = strategy.scan_at(symbol, df_T, T, sym_state)
                all_hits.extend(hits)
            except Exception as exc:
                # 单 T 日异常不中断回放（边界审查）
                _logger.debug("replay 跳过 symbol=%s T=%s 异常=%s",
                              symbol, T, type(exc).__name__)
                continue
        # symbol 处理完：进度上报（每 50 个 + 收尾一次）
        _done += 1
        if progress_cb is not None and (_done % _PROGRESS_EVERY == 0 or _done == total_symbols):
            progress_cb(_done, total_symbols)

    # —— 汇总统计 ——
    if position_model is None:
        position_model = PositionModel()
    stats = _compute_stats(all_hits, position_model)
    recommendation = _recommend_min_rr(stats)

    # n_trading_days：回放区间交易日数（各 symbol 同区间，取首个非空 symbol 的计数）。
    n_trading_days = 0
    for df in price_data.values():
        n_trading_days = len(_iter_trading_days(df.index, start, end))
        if n_trading_days:
            break
    # 年化收益 CAGR = (equity_end/equity_0)^(252/n_trading_days) - 1（equity 归一化 equity_0=1.0）。
    equity_curve = stats.get("equity_curve", [])
    equity_end = equity_curve[-1]["equity"] if equity_curve else 1.0
    if n_trading_days > 0 and equity_end > 0:
        annualized_return = equity_end ** (252.0 / n_trading_days) - 1.0
    else:
        annualized_return = 0.0

    # metadata：策略提供的阈值（颈线法走 id_cfg["min_rr"]；caisen 遗产 cfg.min_rr_ratio 兜底）。
    # P1-5 修复：旧实现只读 strategy.cfg.min_rr_ratio，颈线法无 .cfg → 恒 None。
    _id_cfg = getattr(strategy, "id_cfg", None) or {}
    cfg_threshold = _id_cfg.get("min_rr")
    if cfg_threshold is None:
        _cfg = getattr(strategy, "cfg", None)
        cfg_threshold = getattr(_cfg, "min_rr_ratio", None)

    return ReplayReport(
        n_hits=stats["n_hits"],
        win_rate=stats["win_rate"],
        avg_rr=stats["avg_rr"],
        max_drawdown=stats["max_drawdown"],
        pattern_dist=stats["pattern_dist"],
        monthly_returns=stats["monthly_returns"],
        avg_holding_bars=stats["avg_holding_bars"],
        threshold_recommendation=recommendation,
        equity_curve=equity_curve,
        trades=stats.get("trades", []),
        annualized_return=annualized_return,
        n_trading_days=n_trading_days,
        metadata={
            "hits": all_hits,
            "cfg_min_rr": cfg_threshold,
            "strategy": type(strategy).__name__,
            "position_model": position_model.to_dict(),
        },
    )


# ---------------------------------------------------------------------------
# 统计计算（纯函数，策略中立——只依赖 TRADE_REQUIRED_KEYS 字段）
# ---------------------------------------------------------------------------
def _compute_stats(hits: list, position_model: Optional[PositionModel] = None) -> dict:
    """从命中交易列表计算胜率/平均盈亏比/最大回撤/信号分布/月度收益/平均持仓天数。

    position_model：None → 默认 PositionModel()（pos_cap=5% 加总口径，P0-1）。
    净值曲线委托 backtest.models.build_equity_curve（回测/计算单元共用单源）。

    统计定义：
        - win_rate = 盈利笔数(rr>0) / n_hits；
        - avg_rr = sum(rr) / n_hits；
        - max_drawdown：基于累计 rr 曲线的 peak-to-trough 最大跌幅（负值，0 表示无回撤）；
        - pattern_dist：按 signal_type 计数（fallback pattern_type 兼容阶段A caisen）；
        - monthly_returns：按 entry_date 月份聚合 rr 之和；
        - avg_holding_bars：exit_pos - entry_pos 均值。

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
            "equity_curve": [],
            "trades": [],
        }

    n = len(hits)
    # Layer2 阶段1：hits 现为 list[Signal]（frozen dataclass），改读属性替代 dict 键访问。
    # Signal 字段都有默认值（None / 0 / ""），rr/holding_bars 等数值字段 None 兜底为 0。
    rrs = [float(h.rr) for h in hits]
    wins = sum(1 for r in rrs if r > 0)

    # 累计 rr 曲线 → 最大回撤（peak-to-trough）。
    # 2026-08-02 修复：peak 必须从 0.0 起（净值归一化 equity_0=1.0）。
    # 旧实现 peak=float('-inf') 会把「首笔即亏损」的曲线起点当作峰值，
    # 单笔亏损 rr=-1 的回测显示 max_drawdown=0.0（漏算回撤，误导播报/复盘）。
    cumulative = []
    running = 0.0
    for r in rrs:
        running += r
        cumulative.append(running)
    peak = 0.0
    max_dd = 0.0
    for v in cumulative:
        peak = max(peak, v)
        dd = v - peak   # dd ≤ 0（谷值低于峰值时为负）
        if dd < max_dd:
            max_dd = dd   # 保留最负值（最大回撤）

    # 信号类型分布（阶段A：兼容 caisen 的 pattern_type；阶段D 统一 signal_type）
    pattern_dist: dict = {}
    for h in hits:
        pt = h.signal_type or getattr(h, "pattern_type", None) or "unknown"
        pattern_dist[pt] = pattern_dist.get(pt, 0) + 1

    # 月度收益：按 entry_date 月份聚合 rr
    monthly_returns: dict = {}
    for h in hits:
        ed = h.entry_date
        if ed is None:
            continue
        try:
            ts = pd.Timestamp(ed)
            key = f"{ts.year}-{ts.month:02d}"
        except Exception:
            continue   # 非法日期跳过（不污染聚合）
        monthly_returns[key] = monthly_returns.get(key, 0.0) + float(h.rr)

    # 平均持仓天数
    avg_holding = sum(float(h.holding_bars or 0) for h in hits) / n

    # 买卖流水 trades + 资金曲线 equity_curve（按 exit_date 排序）。
    # equity 模型（P0-1）：PositionModel 单源——默认 pos_cap 单笔仓位加总 + 并发上限；
    # 旧 RISK_FRAC=0.01 复利经 risk_frac 显式启用。年化由 replay() 用 CAGR 算。
    def _iso(v):
        if v is None:
            return ""
        if isinstance(v, pd.Timestamp):
            return v.isoformat()
        return str(v)

    sorted_hits = sorted(hits, key=lambda h: str(h.exit_date or ""))
    trades = [
        {
            "symbol": h.symbol,
            "signal_type": h.signal_type or getattr(h, "pattern_type", None),
            "entry_date": _iso(h.entry_date),
            "entry_price": h.entry_price,
            "exit_date": _iso(h.exit_date),
            "exit_price": h.exit_price,
            "exit_reason": h.exit_reason,
            "rr": h.rr,
            "holding_bars": h.holding_bars,
            "avg_pnl_pct": h.avg_pnl_pct,
        }
        for h in sorted_hits
    ]
    equity_curve = build_equity_curve(trades, position_model or PositionModel())

    return {
        "n_hits": n,
        "win_rate": wins / n,
        "avg_rr": sum(rrs) / n,
        "max_drawdown": max_dd,
        "pattern_dist": pattern_dist,
        "monthly_returns": monthly_returns,
        "avg_holding_bars": avg_holding,
        "equity_curve": equity_curve,
        "trades": trades,
    }


def _recommend_min_rr(stats: dict) -> str:
    """基于回放统计（胜率/平均盈亏比）数据驱动建议生产阈值（阶段D 改名 threshold_recommendation）。

    决策逻辑：
        - 样本不足（n_hits < 5）：建议先用宽松阈值积累样本；
        - EV = 胜率×平均盈亏比 - (1-胜率)×1.0；
          EV>0.2 → 阈值有效；EV∈[0,0.2] → 边际有效适度放宽；EV<0 → 过严或策略无效。
    """
    n = stats["n_hits"]
    wr = stats["win_rate"]
    avg_rr = stats["avg_rr"]

    if n < 5:
        return (
            f"样本不足（命中笔数={n} < 5）：建议先用宽松的盈亏比下限（1.5）积累更多样本，"
            f"待样本量充足后再数据驱动定标。"
        )

    ev = wr * avg_rr - (1 - wr) * 1.0
    if ev > 0.2:
        return (
            f"建议保留当前阈值（期望值={ev:.3f} > 0.2，胜率={wr:.1%}，平均盈亏比={avg_rr:.2f}）。"
            f"当前样本具正期望，高质量过滤有效。"
        )
    if ev >= 0:
        return (
            f"建议适度放宽阈值（期望值={ev:.3f} ∈ [0, 0.2]，胜率={wr:.1%}，"
            f"平均盈亏比={avg_rr:.2f}）。边际有效但样本偏少，可适度下调阈值增加样本。"
        )
    return (
        f"建议更宽松阈值重新评估或回炉优化策略（期望值={ev:.3f} < 0，胜率={wr:.1%}，"
        f"平均盈亏比={avg_rr:.2f}）。当前样本期望为负，阈值过严或策略需优化。"
    )


# ---------------------------------------------------------------------------
# 辅助：交易日迭代（支持 RangeIndex 与 DatetimeIndex，策略中立）
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
    start_pos = index.searchsorted(start, side="left") if not isinstance(index, pd.RangeIndex) \
        else max(int(start) - int(index[0]), 0)
    end_pos = index.searchsorted(end, side="right") if not isinstance(index, pd.RangeIndex) \
        else min(int(end) - int(index[0]) + 1, n)

    start_pos = max(0, min(start_pos, n))
    end_pos = max(0, min(end_pos, n))
    if start_pos >= end_pos:
        return []
    return list(index[start_pos:end_pos])
