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

import pandas as pd

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

    def scan_live(self, symbol: str, df_upto, date) -> list:
        """实盘纯识别：调 detect_neckline_method（df_upto 截至 date），**不调 simulate_exit**。

        与 scan_at 的物理差异（Why 拆两入口）：
            - scan_at 是【回测一站式】：detect + simulate_exit 推进未来 K 线模拟出场
              （simulate_exit 从 T_pos 向前吃 max_holding 根，回测允许读未来）。
            - scan_live 是【实盘纯识别】：只识别形态，不模拟出场。实盘 T-1 晚 _eod 调用时
              根本没有"未来 K 线"可用（未来还没发生），出场由二期引擎 pre_open / stop_loss_monitor
              在交易时段实时做，不需要回测模拟。

        无前视契约：
            df_upto 由 Task7b 的 _eod 从 data_lake 加载该 symbol 截至 date 的前复权日线
            （截断于 date，不含 date 之后），atr 也在 df_upto 上算——严格因果。

        参数：
            symbol: 标的代码（归因用）
            df_upto: 该 symbol 截至 date 的前复权日线 DataFrame（OHLCV，index 为 DatetimeIndex）
            date: 当前识别日（_eod 传入 T-1 收盘日）

        返回：
            Signal dict 列表（仅当日突破的），字段供 signal_runner 消费：
                symbol / formed_at / breakout_date / neckline / bottom / entry_price / atr
            突破日非当日（res["formed_at"] != date）→ 返 []（只挂当日新信号，防历史重吐）。
        """
        # ATR 全序列预算（窗口对齐 id_cfg["window"]，与 scan_at / precompute 同口径）。
        # 物理意图：颈线在 window 天形成，衡量其波动尺度也用 window 天，而非写死 14 天。
        # 截至此处仅用 df_upto（无前视），末根即 date 当日的 ATR。
        atr_full = compute_atr(
            df_upto["high"], df_upto["low"], df_upto["close"], window=self.id_cfg["window"]
        )

        # 识别：detect_neckline_method（df_upto 截至 date，atr_series 末根对齐）。
        # detect 仅在末根突破时返回（内部 close_T = W["close"].iloc[-1] > c_star 才命中），
        # 故 res["formed_at"] == df_upto.index[-1] == date（正常路径）。
        res = detect_neckline_method(df_upto, self.id_cfg, atr_series=atr_full)
        if res is None:
            return []

        # 当日突破过滤（防御层）：只挂当日新信号。
        # Why：detect 物理上只在末根突破时返，此处等于 date 是常态；但显式校验防 detect
        # 内部窗口语义未来变化（如支持历史日回溯）时把旧信号当新信号重吐占仓。
        # detect 没有 breakout_date 字段——突破日即 res["formed_at"]（=W.index[-1]）。
        breakout_date = res.get("formed_at")
        if breakout_date != date:
            return []

        # Signal dict（实盘纯识别字段集，不掺 simulate_exit 的出场字段）。
        # entry_price：优先取 res["entry"]（= 颈线价 c_star 挂单回踩进场），
        # 缺则用 neckline 近似（c_star 本身即颈线，entry 默认 == neckline，兜底防 detect 返回体未来缺 entry）。
        # atr：用 atr_full 末值（对齐 date 当日，供二期引擎算止损=颈线−N×ATR 用）。
        return [{
            "symbol": symbol,
            "signal_type": "neckline",
            "formed_at": res.get("formed_at"),
            "breakout_date": res.get("formed_at"),
            "neckline": res.get("neckline"),
            "bottom": res.get("bottom"),
            "entry_price": res.get("entry") if res.get("entry") is not None else res.get("neckline"),
            "atr": float(atr_full.iloc[-1]) if not pd.isna(atr_full.iloc[-1]) else res.get("atr"),
        }]
