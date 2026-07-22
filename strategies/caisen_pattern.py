# -*- coding: utf-8 -*-
"""caisen 形态策略适配器（阶段A 临时——包原 backtest_replay 形态逻辑，零行为变化）。

物理定位：回测引擎与策略解耦后，caisen 形态（W底/头肩/triangle/zigzag）作为 Strategy
接口的实现。本类把原 execution/backtest_replay.py 的形态专属逻辑（precompute 预算 +
scan_at 截断 screen + _simulate_one_trade 离场）原样搬入，引擎变策略中立。

【阶段E 将随 caisen 形态代码一起删】——届时颈线法（NecklineMethodStrategy）成为唯一策略。
本类存在的意义：阶段A 解耦时保证 caisen 回测行为零变化（strangler 搬运，可验证）。
"""
from __future__ import annotations

import logging
import math
from types import SimpleNamespace
from typing import Optional

import pandas as pd

from caisen.config import StrategyConfig
from caisen.risk import RiskManager
from caisen.patterns.screener import PatternScreener
from caisen import plan as plan_mod
from caisen.patterns.zigzag_causal import causal_pivots, compute_atr
from execution.exit_logic import check_exit, ExitAction, ExitReason  # Task 1.2：exit_logic 迁 execution 包（caisen_pattern 本任务临时保绿，Task 1.3 整删）

from .base import TRADE_REQUIRED_KEYS
from .registry import register_strategy

_logger = logging.getLogger(__name__)


@register_strategy("caisen")
class CaisenPatternStrategy:
    """caisen 形态策略（W底/头肩/triangle）适配器。

    经 Strategy 接口接入回测引擎。precompute 预算全序列 atr/pivots/HV（O(标的×T²)→O(标的×T)
    复用优化）；scan_at 逐 T 截断 screen + plan.generate + 形态签名去重 + _simulate_one_trade
    离场（调 caisen.engines.exit_logic.check_exit，含移动止盈）。

    构造参数：
        cfg:              StrategyConfig 实例（形态识别 + min_rr_ratio + 离场参数）。
        risk:             RiskManager 实例（提供 macro_position_coef）。
        aum:              账户总资金（plan.generate 用）。
        trading_calendar: 可选交易日历（plan.generate 用）。
    """

    def __init__(
        self,
        cfg: StrategyConfig,
        risk: RiskManager,
        aum: float,
        trading_calendar: Optional[pd.DatetimeIndex] = None,
    ):
        self.cfg = cfg
        self.risk = risk
        self.aum = aum
        self.trading_calendar = trading_calendar
        self.screener = PatternScreener(cfg, risk)

    @property
    def config_schema(self) -> type:
        return StrategyConfig

    def precompute(self, symbol: str, full_df: pd.DataFrame) -> dict:
        """预算全序列 atr + pivots + HV（复用优化），返回 strategy_state（含跨 T 去重锚点）。

        因果保证：causal_pivots 纯因果，追加数据下不变；每 T 从 full_pivots 截断
        iloc[:T+1] 并对末尾 confirm_bars 标 0，等价 causal_pivots(df.loc[:T])。
        """
        atr_full = compute_atr(full_df["high"], full_df["low"], full_df["close"])
        pivots = causal_pivots(full_df["close"], atr_full, self.cfg)
        ret = full_df["close"].pct_change(fill_method=None)
        hv = ret.rolling(self.cfg.hv_window).std() * math.sqrt(252)
        return {
            "full_df": full_df,
            "atr_full": atr_full,
            "pivots": pivots,
            "hv": hv,
            "last_sig": None,   # 形态签名去重锚点（跨 T）
        }

    def scan_at(self, symbol: str, df_T: pd.DataFrame, T, strategy_state: dict) -> list:
        """对单 symbol 在 T 日：截断 pivots/HV → screen → plan.generate → 形态签名去重
        → 每 plan _simulate_one_trade → 返回 hit 列表。

        严格无前视：df_T = df.loc[:T]；pivots/hv 从预算全序列 .iloc[:T_pos+1] 截断。
        """
        cfg = self.cfg
        full_df = strategy_state["full_df"]
        sym_index = full_df.index

        if len(df_T) < cfg.min_pattern_bars:
            return []   # 数据不足以形成形态

        T_pos = sym_index.get_loc(T)
        # pivot 复用：截断到 T + confirm_bars 过滤（末尾 confirm_bars 个标 0），等价 causal_pivots(df.loc[:T])
        pivots_T = strategy_state["pivots"].iloc[:T_pos + 1].copy()
        n_pt = len(pivots_T)
        if cfg.confirm_bars > 0 and n_pt > 0:
            pivots_T.iloc[max(0, n_pt - cfg.confirm_bars):] = 0
        # HV 复用：截至 T 的尾部 hv_window 个
        hv_win = strategy_state["hv"].iloc[max(0, T_pos + 1 - cfg.hv_window):T_pos + 1]

        candidates = self.screener.screen_with_pivots(
            {symbol: df_T}, {symbol: pivots_T}, {symbol: hv_win}, T,
        )
        if candidates.empty:
            return []
        plans = plan_mod.generate(
            candidates, cfg, self.risk, self.aum, T, self.trading_calendar,
        )

        # 形态签名去重：同形态（neckline+bottom 不变）连续 T 日只模拟首次
        hits = []
        for p in plans:
            sig = (round(p.neckline_price, 6), round(p.bottom_price, 6))
            if sig == strategy_state["last_sig"]:
                continue
            strategy_state["last_sig"] = sig   # 标记该形态已处理（无论成交与否）
            hit = self._simulate_one_trade(full_df, p, T, cfg)
            if hit is not None:
                hits.append(hit)
        return hits

    # -----------------------------------------------------------------------
    # 离场模拟（原 backtest_replay._simulate_one_trade 原样搬入，零行为变化）
    # -----------------------------------------------------------------------
    def _simulate_one_trade(self, df: pd.DataFrame, p, entry_day, cfg) -> Optional[dict]:
        """对单个 TradePlan 模拟 T+1 回踩成交 + 后续止盈/止损/时间止损离场。

        离场逻辑（逐日推进，优先级：stop_loss > take_profit_2x > take_profit > timeout）：
            T+1：若 low≤entry_upper 且 high≥entry_lower → 成交（entry_price=entry_upper）；
            T+2..：逐日调 caisen.engines.exit_logic.check_exit 判离场（移动止盈激活后
                   stop 上移至 entry 锁定本金）；超 max_holding_bars 且浮盈<timeout_exit_threshold
                   → 时间止损砍亏；序列末尾未离场 → still_open（按末根 close 记浮盈）。

        【Step4b 单源】离场判定统一调 check_exit，消除与实盘 ExecutionEngine 的双源真理。
        max_holding 用位置计数（entry_pos + cfg.max_holding_bars），稳健于任意 index 类型。
        """
        max_holding_bars = cfg.max_holding_bars
        idx = df.index
        try:
            entry_pos = idx.get_loc(entry_day)
        except KeyError:
            return None   # entry_day 不在 index（数据异常），跳过
        if entry_pos + 1 >= len(idx):
            return None   # 无 T+1 数据，无法判定回踩触发
        next_pos = entry_pos + 1

        # —— T+1 回踩触发判定 ——
        row_t1 = df.iloc[next_pos]
        high_t1 = float(row_t1["high"])
        low_t1 = float(row_t1["low"])
        if not (low_t1 <= p.entry_upper and high_t1 >= p.entry_lower):
            return None   # T+1 未触及回踩区间，未成交
        entry_price = p.entry_upper

        # —— 后续逐日推进离场判定（T+1 当日也可离场，T+2..）——
        exit_price = None
        exit_reason = None
        exit_pos = None

        max_hold_pos = entry_pos + max_holding_bars

        # 移动止盈本地状态：check_exit 激活 trailing 后返回 new_stop，本循环据此更新 cur_stop
        cur_stop = p.stop_loss
        cfg_view = SimpleNamespace(
            trailing_to_breakeven=cfg.trailing_to_breakeven,
            trailing_activation_bars=cfg.trailing_activation_bars,
            max_holding_bars=max_holding_bars,
            timeout_exit_threshold=p.timeout_exit_threshold,
        )

        for pos in range(next_pos, len(idx)):
            row = df.iloc[pos]
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            bars_held = pos - entry_pos

            pos_dict = {
                "entry": entry_price,
                "stop": cur_stop,
                "take_profit": p.take_profit,
                "take_profit_2x": p.take_profit_2x,
            }
            bar_dict = {"high": high, "low": low, "close": close}
            decision = check_exit(pos_dict, bar_dict, bars_held=bars_held, cfg=cfg_view)

            # 移动止盈：new_stop 有值 → 上移本地 cur_stop（止损只上移）
            if decision.new_stop is not None and decision.new_stop > cur_stop:
                cur_stop = decision.new_stop

            if decision.action == ExitAction.HOLD:
                continue

            # CLOSE：按 reason 决定 exit_price
            if decision.reason == ExitReason.STOP_LOSS:
                exit_price = cur_stop
                exit_reason = "stop_loss"
            elif decision.reason == ExitReason.TAKE_PROFIT:
                if p.take_profit_2x is not None and high >= p.take_profit_2x:
                    exit_price = p.take_profit_2x
                else:
                    exit_price = p.take_profit
                exit_reason = "take_profit"
            elif decision.reason == ExitReason.TIMEOUT:
                exit_price = close
                exit_reason = "timeout"
            else:
                exit_price = close
                exit_reason = "timeout"
            exit_pos = pos
            break

        # 序列末尾仍未离场 → still_open
        if exit_price is None:
            last_row = df.iloc[-1]
            exit_price = float(last_row["close"])
            exit_reason = "still_open"
            exit_pos = len(idx) - 1

        # rr = (exit-entry) / (entry-stop)
        risk_per_unit = entry_price - p.stop_loss
        if risk_per_unit <= 0:
            return None
        rr = (exit_price - entry_price) / risk_per_unit

        return {
            "symbol": p.symbol,
            "signal_type": p.pattern_type,     # 标准字段（颈线法=neckline；caisen=形态名）
            "pattern_type": p.pattern_type,    # 向后兼容 _compute_stats（阶段D 统一为 signal_type）
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
