# -*- coding: utf-8 -*-
"""颈线法策略适配器（NecklineMethodStrategy · 阶段B）。

挂 scripts/neckline_method_v0.py + scripts/neckline_backtest.py 到 strategies/ 包，
经 Strategy 接口接入解耦后的回测引擎。颈线法的进场/出场是完整状态机（simulate_exit：
挂单回踩 + max_wait + cancel_on 撤单 + 分级止盈 tp1/tp2 + 超时），scan_at 一站式产出
trade dict（出场逻辑归策略侧，引擎零感知）。

与 caisen 形态的语义差异（已在 scan_at 处理）：
    - 信号去重：颈线法用 cooldown 交易日窗（caisen 用 neckline+bottom 价对签名）
    - 进场：挂单回踩 max_wait 天（caisen T+1 回踩成交）
    - 出场：分级止盈 tp1_portion 加权（caisen 单笔全平 check_exit）
    - rr：颈线法 avg_pnl_pct/risk_pct（caisen (exit-entry)/(entry-stop)）——同口径"风险倍数"
"""
from __future__ import annotations

import os
import sys

# scripts/ 加入 sys.path（neckline_method_v0/neckline_backtest 在 scripts/）
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_PROJ_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from neckline_method_v0 import detect_neckline_method, DEFAULTS, compute_atr  # noqa: E402
from neckline_backtest import simulate_exit, EXEC_DEFAULTS  # noqa: E402

from .neckline_schema import NecklineConfig
from .registry import register_strategy

# 识别层 / 执行层 键集（cfg_override 拆分用）
_NECKLINE_ID_KEYS = (
    "window", "min_touches", "min_suppression", "local_extrema_window", "min_bottoms",
    "breakout_vol_mult", "min_rr", "max_h_atr", "stop_atr_mult", "tp_h_mult", "decay_tau",
)
_NECKLINE_EXEC_KEYS = (
    "max_holding", "max_wait", "cooldown", "buy_limit_atr_mult",
    "tp1_h_mult", "tp1_portion", "cancel_thresh_mult",
)


@register_strategy("neckline")
class NecklineMethodStrategy:
    """颈线法策略（挂单回踩 + 分级止盈 + 撤单）。

    构造：
        cfg_override: 18 维参数覆盖 dict（键在 NecklineConfig.model_fields 内）。
    """

    def __init__(self, cfg_override: dict | None = None, **kwargs):
        ov = cfg_override or {}
        self.id_cfg = {**DEFAULTS, **{k: ov[k] for k in _NECKLINE_ID_KEYS if k in ov}}
        self.exec_cfg = {**EXEC_DEFAULTS, **{k: ov[k] for k in _NECKLINE_EXEC_KEYS if k in ov}}
        self._last_signal_pos: dict = {}   # per-symbol cooldown 锚点（跨 T）

    @property
    def config_schema(self) -> type:
        return NecklineConfig

    def precompute(self, symbol: str, full_df) -> dict:
        """预算全序列 ATR（窗口对齐 id_cfg["window"]），scan_at 复用截断。"""
        atr_full = compute_atr(
            full_df["high"], full_df["low"], full_df["close"], window=self.id_cfg["window"]
        )
        return {"atr_full": atr_full, "full_df": full_df}

    def scan_at(self, symbol: str, df_T, T, strategy_state: dict) -> list:
        """对单 symbol 在 T 日：detect_neckline_method → simulate_exit → 标准 trade dict。

        严格无前视：detect 用 df_T（=df.loc[:T]）；atr 用预算全序列 .iloc[:T_pos+1] 截断。
        cooldown 去重：相邻信号（T_pos 差 < cooldown）只处理首次。
        """
        full_df = strategy_state["full_df"]
        atr_full = strategy_state["atr_full"]
        sym_index = full_df.index
        T_pos = sym_index.get_loc(T)

        # cooldown 去重：距上次信号不足 cooldown 交易日 → 跳过
        last = self._last_signal_pos.get(symbol)
        if last is not None and T_pos - last < self.exec_cfg["cooldown"]:
            return []

        # 识别：detect_neckline_method(df_T, id_cfg, atr 截断) —— 严格无前视
        res = detect_neckline_method(df_T, self.id_cfg, atr_series=atr_full.iloc[:T_pos + 1])
        if res is None:
            return []

        # 出场：simulate_exit 从 T_pos 推进 max_holding 根，需 full_df（推进用未来 K 线，属回测允许）
        sim = simulate_exit(
            full_df, T_pos, res["neckline"], res["bottom"], float(atr_full.iloc[T_pos]),
            exec=self.exec_cfg, id_cfg=self.id_cfg,
        )
        # 消费信号（无论成交与否，cooldown 锚点更新，防同形态连续 T 重复计）
        self._last_signal_pos[symbol] = T_pos

        # 未成交 / 撤单 → 不计入 hits（exit_reason 标识）
        if sim is None or sim["exit_reason"] in ("skip_no_pullback", "skip_target_met"):
            return []

        # rr 口径对齐 caisen：颈线法 avg_pnl_pct(%) / risk_pct(%) = 风险倍数。
        # 边界：entry≤stop（跳空低开过止损，risk_pct≤0）→ 用 avg_pnl 符号兜底（防 rr 符号反转）。
        risk_pct = sim.get("risk_pct")
        if risk_pct and risk_pct > 0:
            rr = sim["avg_pnl_pct"] / risk_pct
        else:
            rr = sim["avg_pnl_pct"] / 100.0

        return [{
            "symbol": symbol,
            "signal_type": "neckline",
            "formed_at": T,
            "entry_date": sim.get("buy_date", T),
            "entry_price": sim["entry"],
            "exit_date": sim.get("exit_date"),
            "exit_price": sim.get("exit_price"),
            "exit_reason": sim["exit_reason"],
            "rr": rr,
            "holding_bars": sim.get("holding_bars", 0),
            # 颈线法附加字段（详情展示用，统计层不依赖）
            "neckline": sim.get("neckline"),
            "avg_pnl_pct": sim.get("avg_pnl_pct"),
        }]
