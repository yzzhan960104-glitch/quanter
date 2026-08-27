# -*- coding: utf-8 -*-
"""R6-10 · 单笔盈利深度调研（用户裁决方向「提高单笔盈利」 · 2026-08-26）。

三部分：
  A. 冠军逐笔解剖：单笔 +1.07% 由什么构成（出场原因/持有期/形态特征分桶）；
  B. 滑点淹没实锤：0-50bps 滑点矩阵 × 组合年化 + 盈亏平衡滑点；
  C. 单笔-频率-组合 frontier：质量闸变体（min_rr/supp/buy_limit/max_holding/tp_h）
     的 (日均笔数, 平均单笔, 组合年化@25bps滑点)——找「高单笔」与「组合仍赢」
     的帕累托点。

产物：logs/r6_10_per_trade_frontier.json + 控制台报告。
"""
import json
import os
import sys
import time
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd


def main():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.objective import run_full_scan, portfolio_metrics
    from backtest.models import PositionModel

    base = json.loads(open("logs/r6_8_loop/state.json", encoding="utf-8").read())["base"]
    universe, _ = freeze("2021-01-01")
    split = holdout_split()
    udates = next(iter(universe.values())).index
    out = {}

    # ════════ A. 冠军逐笔解剖（outer 2026 段 + 全历史对照）════════
    t0 = time.time()
    filled = run_full_scan(base, universe)
    print(f"[A·scan] n={len(filled)} ({time.time()-t0:.0f}s)", flush=True)
    outer = [r for r in filled if split.outer.covers(pd.Timestamp(r["signal_date"]))]

    def bucket(rows, key_fn, buckets, tag):
        print(f"\n  [{tag}]", flush=True)
        res = {}
        for label, lo, hi in buckets:
            sub = [r for r in rows if lo <= (key_fn(r) or 0) < hi]
            if not sub:
                continue
            ps = [r["avg_pnl_pct"] for r in sub]
            res[label] = {"n": len(sub), "avg": round(sum(ps)/len(ps), 2),
                          "win": round(sum(1 for p in ps if p > 0)/len(ps), 3)}
            print(f"    {label:>14}: n={len(sub):>5} avg={res[label]['avg']:+6.2f}% "
                  f"胜率={res[label]['win']:.0%}", flush=True)
        return res

    print("=== A. 冠军逐笔解剖（outer 2026 段 9,440 笔）===", flush=True)
    reasons = {}
    for reason in ("stop_loss", "tp1", "tp2", "timeout"):
        sub = [r for r in outer if r["exit_reason"] == reason]
        if sub:
            ps = [r["avg_pnl_pct"] for r in sub]
            reasons[reason] = {"n": len(sub), "avg": round(sum(ps)/len(ps), 2)}
            print(f"    出场 {reason:>10}: n={len(sub):>5} "
                  f"avg={reasons[reason]['avg']:+6.2f}%", flush=True)
    hb = bucket(outer, lambda r: r.get("holding_bars"),
                [("[0,3]", 0, 4), ("[4,7]", 4, 8), ("[8,12]", 8, 13),
                 ("[13,20]", 13, 21), ("[21,∞]", 21, 1e9)], "持有天数桶")
    hatr = bucket(outer, lambda r: r.get("H_over_ATR"),
                  [("浅[0,2)", 0, 2), ("[2,3.5)", 2, 3.5), ("[3.5,4.5)", 3.5, 4.5),
                   ("深[4.5,∞)", 4.5, 1e9)], "形态深度 H/ATR 桶")
    rrb = bucket(outer, lambda r: r.get("rr"),
                 [("[2,2.5)", 2, 2.5), ("[2.5,3)", 2.5, 3), ("[3,4)", 3, 4),
                   ("[4,∞)", 4, 1e9)], "盈亏比 rr 桶")
    out["A"] = {"reasons": reasons, "holding": hb, "h_atr": hatr, "rr": rrb}

    # ════════ B. 滑点矩阵 + 盈亏平衡 ════════
    print("\n=== B. 滑点敏感性（outer 组合年化，双边 bps）===", flush=True)
    slip_rows = {}
    for bps in (0, 5, 10, 15, 25, 50):
        m = portfolio_metrics(outer, split.outer, udates,
                              position_model=PositionModel(slippage_bps=float(bps)))
        slip_rows[bps] = round(m["ann"], 3)
        print(f"    slip {bps:>3}bps: ann={m['ann']:+8.1%}  equity_end={m['equity_end']:.3f}",
              flush=True)
    be = None
    for i in range(len(slip_rows)):
        b = list(slip_rows)[i]
        if slip_rows[b] <= 0:
            be = list(slip_rows)[i]
            break
    print(f"    → 年化归零的滑点档位：≈{be}bps（相邻档间）", flush=True)
    out["B"] = {"slip_ann": slip_rows, "breakeven_bps": be}

    # ════════ C. frontier：质量闸变体 ════════
    print("\n=== C. 单笔-频率-组合 frontier（outer 段，滑点统一 25bps）===", flush=True)
    variants = [
        ("champion", {}),
        ("min_rr=2.5", {"min_rr": 2.5}),
        ("min_rr=3.0", {"min_rr": 3.0}),
        ("supp=0.4", {"min_suppression": 0.4}),
        ("supp=0.5", {"min_suppression": 0.5}),
        ("bl=3.0", {"buy_limit_atr_mult": 3.0}),
        ("mh=50", {"max_holding": 50}),
        ("tp_h=2.0", {"tp_h_mult": 2.0}),
        ("tp_h=2.5", {"tp_h_mult": 2.5}),
        ("cd=3", {"cooldown": 3}),
    ]
    pm25 = PositionModel(slippage_bps=25.0)
    fr = {}
    for tag, ov in variants:
        p = {**deepcopy(base), **ov}
        t1 = time.time()
        fl = run_full_scan(p, universe)
        ot = [r for r in fl if split.outer.covers(pd.Timestamp(r["signal_date"]))]
        if not ot:
            continue
        ps = [r["avg_pnl_pct"] for r in ot]
        m = portfolio_metrics(ot, split.outer, udates, position_model=pm25)
        fr[tag] = {"n_outer": len(ot), "per_day": round(len(ot)/156, 1),
                   "avg": round(sum(ps)/len(ps), 2),
                   "median": round(sorted(ps)[len(ps)//2], 2),
                   "ann_s25": round(m["ann"], 3)}
        print(f"    [{tag:>12}] {fr[tag]['per_day']:>5.1f}笔/日 avg={fr[tag]['avg']:+6.2f}% "
              f"中位={fr[tag]['median']:+6.2f}% ann@25bps={fr[tag]['ann_s25']:+8.1%} "
              f"({time.time()-t1:.0f}s)", flush=True)
    out["C"] = fr

    json.dump(out, open("logs/r6_10_per_trade_frontier.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("\n[done] logs/r6_10_per_trade_frontier.json", flush=True)


if __name__ == "__main__":
    main()
