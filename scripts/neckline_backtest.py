# -*- coding: utf-8 -*-
"""颈线法多标的回测：流动性 top30 标的 × 颈线法识别 × 挂单回踩持有期模拟。

用户交易规则（v2 · 2026-07-18）：
    进场 = 突破后在 颈线+1·ATR 挂买单，有效期 max_wait 天，回踩触发即按挂单价成交；
           不回踩则放弃（防追高，但可能错过直接飞的信号）。
    止损 = 颈线−1·ATR（破位 1ATR 确认，防假摔洗盘）。
    止盈 = 分级 50%@颈线+H，50%@颈线+2H。
    超时 = 成交后 max_holding 日未达任一止盈，收盘卖剩余。

盈亏结构：risk=entry−stop=2·ATR（固定对称），收益=H−ATR / 2H−ATR，
盈亏比由形态深度 H/ATR 决定。

用法：
    PYTHONIOENCODING=utf-8 python -u scripts/neckline_backtest.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from neckline_method_v0 import detect_neckline_method, DEFAULTS, compute_atr


# 持有期参数（用户规则，可调）
MAX_HOLDING = 15           # 成交后超时持仓周期（交易日）
MAX_WAIT = 5               # 挂单等待回踩成交有效期（≈一周）
COOLDOWN = 5               # 信号去重冷却期（相邻信号合并为一次，同形态多日触发）
TOP_N = 30                 # 扫描流动性 top N 标的


def simulate_exit(sym_df: pd.DataFrame, signal_idx: int, c_star: float,
                  bottom: float, atr_val: float,
                  max_holding: int = MAX_HOLDING, max_wait: int = MAX_WAIT):
    """挂单回踩进场 + 颈线−ATR 止损 + 分级止盈。

    执行：① 挂买单@颈线+ATR，max_wait 天内首个 low≤挂单价 的日按挂单价成交，
          未回踩返 skip；② 成交后挂止损@颈线−ATR + 止盈@颈线+H/+2H，逐根判 exit。
    """
    H = c_star - bottom
    if H <= 0:
        return None
    buy_limit = c_star + atr_val    # 挂单价
    stop = c_star - atr_val          # 止损（颈线−ATR）
    tp1 = c_star + H                # 第一止盈（50%仓）——v3 回退：v2 调近致盈亏比崩，恢复 2H 口径
    tp2 = c_star + 2 * H            # 第二止盈（50%仓，主止盈）

    # ① 等回踩成交（用户逻辑修正：等待期 high≥tp1 → 涨幅已兑现，回踩是退潮，撤单）
    wait_end = min(signal_idx + max_wait, len(sym_df) - 1)
    buy_idx = None
    for i in range(signal_idx + 1, wait_end + 1):
        # 等待期价格已涨到第一止盈 → 可确定涨幅结束，撤单不买（过滤"猛突破后回踩"陷阱）
        if float(sym_df["high"].iloc[i]) >= tp1:
            return {"signal_date": sym_df.index[signal_idx].date(),
                    "exit_reason": "skip_target_met",
                    "avg_pnl_pct": 0.0, "lot1_pnl_pct": 0.0, "lot2_pnl_pct": 0.0,
                    "neckline": round(c_star, 3), "entry": None,
                    "risk_pct": None, "tp1": round(tp1, 3), "tp2": round(tp2, 3)}
        if float(sym_df["low"].iloc[i]) <= buy_limit:
            buy_idx = i
            break
    if buy_idx is None:
        return {"signal_date": sym_df.index[signal_idx].date(),
                "exit_reason": "skip_no_pullback",
                "avg_pnl_pct": 0.0, "lot1_pnl_pct": 0.0, "lot2_pnl_pct": 0.0,
                "neckline": round(c_star, 3), "entry": None,
                "risk_pct": None, "tp1": round(tp1, 3), "tp2": round(tp2, 3)}

    entry = min(buy_limit, float(sym_df["open"].iloc[buy_idx]))
    # 限价买单成交价：open>buy_limit（盘中回踩）→ 成交 buy_limit；open<=buy_limit（跳空低开）
    # → 成交 open（市价<挂单价，更优）。旧版 entry=buy_limit 高估了跳空低开的买入价。
    end_idx = min(buy_idx + max_holding, len(sym_df) - 1)

    # ② 持有期逐根判 exit
    lot1_open, lot2_open = True, True
    lot1_pnl, lot2_pnl = None, None
    exit_reason = "timeout"
    for i in range(buy_idx, end_idx + 1):
        row = sym_df.iloc[i]
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        is_last = (i == end_idx)
        if low <= stop:                                  # 优先级1：止损
            lot1_pnl = lot2_pnl = (stop - entry) / entry
            lot1_open = lot2_open = False
            exit_reason = "stop_loss"
            break
        if lot2_open and high >= tp2:                    # 优先级2：tp2（lot1 同日一并卖）
            lot2_pnl = (tp2 - entry) / entry
            lot2_open = False
            if lot1_open:
                lot1_pnl = (tp1 - entry) / entry
                lot1_open = False
            exit_reason = "tp2"
            break
        if lot1_open and high >= tp1:                    # 优先级3：tp1（卖 lot1）
            lot1_pnl = (tp1 - entry) / entry
            lot1_open = False
            continue
        if is_last:                                       # 超时
            if lot1_open:
                lot1_pnl = (close - entry) / entry
            if lot2_open:
                lot2_pnl = (close - entry) / entry
            exit_reason = "timeout"
    if lot1_pnl is None or lot2_pnl is None:
        return None

    avg_pnl = (lot1_pnl + lot2_pnl) / 2
    return {
        "signal_date": sym_df.index[signal_idx].date(),
        "buy_date": sym_df.index[buy_idx].date(),
        "neckline": round(c_star, 3),
        "entry": round(entry, 3),
        "risk_pct": round((entry - stop) / entry * 100, 2),
        "tp1": round(tp1, 3), "tp2": round(tp2, 3),
        "H_over_ATR": round(H / atr_val, 2) if atr_val > 0 else None,
        "lot1_pnl_pct": round(lot1_pnl * 100, 2),
        "lot2_pnl_pct": round(lot2_pnl * 100, 2),
        "avg_pnl_pct": round(avg_pnl * 100, 2),
        "exit_reason": exit_reason,
    }


def dedup_signals(signals, cooldown=COOLDOWN):
    """信号去重：相邻信号（idx 差 < cooldown）只保留第一个。

    物理意图：同一颈线被连续多日触发（滚动 replay 每个突破日都信号），实盘只应
    交易一次。冷却期内重复信号合并，避免同形态多计。
    """
    if not signals:
        return []
    deduped = [signals[0]]
    for sig_idx, res in signals[1:]:
        if sig_idx - deduped[-1][0] >= cooldown:
            deduped.append((sig_idx, res))
    return deduped


def scan_symbol(sym_df, window):
    """对单标的滚动识别 + 去重 + 模拟，返回成交结果列表与统计。"""
    # 预算全序列 ATR 一次（窗口对齐颈线识别窗口，尺度统一），detect 复用
    atr_full = compute_atr(sym_df["high"], sym_df["low"], sym_df["close"], window=window)
    signals = []
    for i in range(window, len(sym_df)):
        res = detect_neckline_method(sym_df.iloc[: i + 1], DEFAULTS,
                                     atr_series=atr_full.iloc[: i + 1])
        if res is not None:
            signals.append((i, res))
    signals = dedup_signals(signals)
    filled = []
    n_skip = 0
    for sig_idx, res in signals:
        sim = simulate_exit(sym_df, sig_idx, res["neckline"], res["bottom"], res["atr"])
        if sim is None:
            continue
        if sim["exit_reason"] in ("skip_no_pullback", "skip_target_met"):
            n_skip += 1
        else:
            # 附识别特征供深挖（突破质量/颈线压制/形态深度）
            vol_T = float(sym_df["volume"].iloc[sig_idx])
            vol5 = float(sym_df["volume"].iloc[max(0, sig_idx - 5):sig_idx].mean()) if sig_idx >= 5 else vol_T
            sim["breakout_vol_ratio"] = round(vol_T / vol5, 2) if vol5 > 0 else 0.0
            sim["suppression"] = res.get("suppression")
            sim["H_over_ATR"] = res.get("H_over_ATR")
            filled.append(sim)
    return filled, len(signals), n_skip


def main():
    lake_path = "data_lake/a_shares_daily.parquet"
    print(f"加载 {lake_path} ...")
    lake = pd.read_parquet(lake_path)
    window = DEFAULTS["window"]

    # 1. 全市场流动性过滤（amount 千元；近30日均 ≥ 1e5 千元 = 1 亿元，对齐 caisen）
    print("计算流动性（近30日均成交额 ≥ 1 亿元）...")
    syms = lake.index.get_level_values("symbol").unique().tolist()
    tradable = []
    for s in syms:
        try:
            amt_v = float(lake.xs(s, level="symbol")["amount"].tail(30).mean())
        except Exception:
            continue
        if amt_v >= 1e5:   # 1 亿元（千元单位）
            tradable.append((s, amt_v))
    tradable.sort(key=lambda x: x[1], reverse=True)
    print(f"可交易标的: {len(tradable)} 只 / 全市场 {len(syms)}")
    print(f"参数（窗口={window}, 挂单等待={MAX_WAIT}, 超时={MAX_HOLDING}, 冷却={COOLDOWN}）\n")

    # 2. 逐标的扫描（全市场，带进度）
    per_sym = []
    all_filled = []
    for idx, (sym, _) in enumerate(tradable):
        if idx % 50 == 0:
            print(f"  进度 {idx}/{len(tradable)} ({idx/len(tradable)*100:.0f}%) ...", flush=True)
        try:
            sym_df = lake.xs(sym, level="symbol").sort_index()
            filled, n_sig, n_skip = scan_symbol(sym_df, window)
        except Exception:
            continue
        pnls = [r["avg_pnl_pct"] for r in filled]
        wins = sum(1 for p in pnls if p > 0)
        n = len(filled)
        per_sym.append({
            "symbol": sym, "n_signals": n_sig, "n_filled": n, "n_skip": n_skip,
            "win_rate": wins / n if n else 0.0,
            "avg_pnl": sum(pnls) / n if n else 0.0,
            "total_pnl": sum(pnls),
        })
        for r in filled:
            r["symbol"] = sym
        all_filled.extend(filled)

    # 存 csv 供后续分析（标的筛选 / 参数调优）
    os.makedirs("logs", exist_ok=True)
    if all_filled:
        pd.DataFrame(all_filled).to_csv("logs/neckline_fullscan_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(per_sym).to_csv("logs/neckline_fullscan_summary.csv", index=False, encoding="utf-8-sig")
    print(f"\n逐笔明细 → logs/neckline_fullscan_trades.csv ({len(all_filled)} 笔)")
    print(f"各标的汇总 → logs/neckline_fullscan_summary.csv ({len(per_sym)} 只)\n")

    # 3. 汇总
    from collections import Counter
    print(f"=== 颈线法全市场汇总（可交易 {len(tradable)} 只）===")
    total = len(all_filled)
    if total == 0:
        print("无成交信号")
        return
    total_wins = sum(1 for r in all_filled if r["avg_pnl_pct"] > 0)
    all_pnls = [r["avg_pnl_pct"] for r in all_filled]
    print(f"总成交笔数 = {total}")
    print(f"整体胜率   = {total_wins/total*100:.1f}%  ({total_wins}/{total})")
    print(f"平均收益   = {sum(all_pnls)/total:.2f}%")
    print(f"中位收益   = {sorted(all_pnls)[total//2]:.2f}%")
    print(f"总收益求和 = {sum(all_pnls):.1f}%")

    print(f"\nexit 分布:")
    rc = Counter(r["exit_reason"] for r in all_filled)
    for reason, cnt in rc.most_common():
        sub = [r["avg_pnl_pct"] for r in all_filled if r["exit_reason"] == reason]
        print(f"  {reason:10s}: {cnt:>4}笔 ({cnt/total*100:>4.0f}%)  avg={sum(sub)/len(sub):+.2f}%")

    print(f"\n=== 各标的（按 avg 收益降序）===")
    print(f"{'symbol':<12}{'信号':>5}{'成交':>5}{'放弃':>5}{'胜率':>7}{'均收益':>8}{'总收益':>9}")
    for s in sorted(per_sym, key=lambda x: x["avg_pnl"], reverse=True):
        print(f"{s['symbol']:<12}{s['n_signals']:>5}{s['n_filled']:>5}{s['n_skip']:>5}"
              f"{s['win_rate']*100:>6.0f}%{s['avg_pnl']:>+7.2f}%{s['total_pnl']:>+8.1f}%")

    # 标的级胜率分布（样本≥3 才有意义）
    valid = [s for s in per_sym if s["n_filled"] >= 3]
    if valid:
        wrs = [s["win_rate"] for s in valid]
        profitable = sum(1 for s in valid if s["avg_pnl"] > 0)
        print(f"\n=== 标的级分布（成交≥3笔的 {len(valid)} 只）===")
        print(f"胜率: max={max(wrs)*100:.0f}%  中位={sorted(wrs)[len(wrs)//2]*100:.0f}%  min={min(wrs)*100:.0f}%")
        print(f"avg收益为正: {profitable}/{len(valid)} 只")


if __name__ == "__main__":
    main()
