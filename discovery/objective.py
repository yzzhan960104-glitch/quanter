# -*- coding: utf-8 -*-
"""L1 评估函数（spec §6.2，Plan 1 简化：inner/outer 二段，无 walk-forward）。

核心范式（探查脚本 probe_champion_oos.py 验证过）：全历史跑 scan_symbol 一次 → 收集
all_filled → 按 signal_date 分段。不硬切 df（scan_symbol 用 sym_df.iloc[:i+1] 截至信号
历史识别，传完整 sym_df 才保 window/ATR 预热）；分段只发生在 signal_date 维度，无前视。

信息隔离（spec §6.2）：inner/outer 从同一客观 all_filled 派生（同 params 同 universe 的
全部信号），但 outer 的 metrics 不反馈任何选择——由调用方（judging/cli）保证只用 inner
排序、outer 只进报告。Plan 1 无搜索，"隔离"主要约束 outer 不进冠军排序。
"""
import pandas as pd

from strategies.neckline.method_v0 import DEFAULTS
from strategies.neckline.backtest import scan_symbol, risk_metrics, EXEC_DEFAULTS

# 21 维参数分层键名（与 scripts/param_iter.PARAM_SPACE 同源；Plan 1 只需键名分层，不需候选值）
ID_KEYS = ["window", "min_touches", "min_suppression", "local_extrema_window",
           "min_bottoms", "breakout_vol_mult", "min_rr", "max_h_atr",
           "stop_atr_mult", "tp_h_mult", "decay_tau"]
EXEC_KEYS = ["max_holding", "max_wait", "cooldown", "buy_limit_atr_mult",
             "tp1_h_mult", "tp1_portion", "cancel_thresh_mult",
             "trailing_grace", "trailing_step", "trailing_floor"]


def run_full_scan(params, universe):
    """全历史跑一组参数 → all_filled（每条带 avg_pnl_pct/signal_date/symbol）。

    显式构造 id_cfg/exec_cfg 传入 scan_symbol（与 param_iter.run_one 同款，去全局 mutation）。
    遍历 universe 调 scan_symbol——单标的用 sym_df 全历史，保证 window/ATR 预热完整。
    """
    id_cfg = {**DEFAULTS, **{k: params[k] for k in ID_KEYS}}
    exec_cfg = {**EXEC_DEFAULTS, **{k: params[k] for k in EXEC_KEYS}}
    window = id_cfg["window"]
    all_filled = []
    for sym, sym_df in universe.items():
        try:
            filled, _n_sig, _n_skip = scan_symbol(sym_df, window, exec=exec_cfg, id_cfg=id_cfg)
            for r in filled:
                r["symbol"] = sym
            all_filled.extend(filled)
        except Exception:
            continue
    return all_filled


def metrics_of(pairs):
    """[(pnl, date), ...] → 风险指标 dict（含 calmar=ann/max_dd，分层裁判 L1 主目标）。"""
    if not pairs:
        return dict(n=0, ann=0.0, sharpe=0.0, max_dd=0.0, kelly=0.0, calmar=0.0, curve=1.0)
    pnls = [p for p, _ in pairs]
    dates = [d for _, d in pairs]
    kelly, curve, ann, sharpe, max_dd = risk_metrics(pnls, dates)
    # calmar = ann/max_dd；max_dd 极小时 ann>0 给 inf（极佳），ann≤0 给 0
    if max_dd > 1e-9:
        calmar = ann / max_dd
    else:
        calmar = float("inf") if ann > 0 else 0.0
    return dict(n=len(pnls), ann=ann, sharpe=sharpe, max_dd=max_dd, kelly=kelly,
                calmar=calmar, curve=curve)


def segment_metrics(all_filled, segment, embargo_days=0):
    """从 all_filled 按 signal_date 过滤到 segment（embargo 偏移）→ metrics dict。

    embargo：segment.start + embargo_days 天内的 signal_date 不计入（吸收前段持仓
    跨越——颈线法 trailing grace/max_holding 可达数日~20 日，inner 末信号持仓可能跨到
    outer 初；embargo 让 outer 评估跳过这段，防 inner 持仓污染 outer 信号统计）。
    """
    from datetime import timedelta
    embargo_cutoff = segment.start + timedelta(days=embargo_days)
    pairs = []
    for r in all_filled:
        d = pd.to_datetime(r["signal_date"])
        if embargo_days > 0 and d.date() < embargo_cutoff:
            continue
        if segment.covers(d):
            pairs.append((r["avg_pnl_pct"], d))
    return metrics_of(pairs)


def evaluate(params, universe, split):
    """评估给定 params 的 inner/outer 两段。

    跑一次 run_full_scan（同 params 同 universe 的客观信号），分 inner(2025)/outer(2026)
    两段。信息隔离：返回的 outer metrics 仅供报告，调用方不得用于冠军排序/选择（spec §6.2）。
    """
    all_filled = run_full_scan(params, universe)
    return {
        "inner": segment_metrics(all_filled, split.inner, embargo_days=0),
        "outer": segment_metrics(all_filled, split.outer, embargo_days=split.embargo_days),
        "n_total": len(all_filled),
    }
