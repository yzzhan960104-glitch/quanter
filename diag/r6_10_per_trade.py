# -*- coding: utf-8 -*-
"""R6-10 · +515% 口径拆解与逐笔统计（用户问询 · 2026-08-26 凌晨）。

回答两问：①+515% 怎么算的（组合口径年化链路）②平均每笔收益多少。
产物 logs/r6_10_per_trade.json。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd


def main():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.objective import run_full_scan, portfolio_metrics

    base = json.loads(open("logs/r6_8_loop/state.json", encoding="utf-8").read())["base"]
    universe, _ = freeze("2021-01-01")
    split = holdout_split()
    udates = next(iter(universe.values())).index

    t0 = time.time()
    filled = run_full_scan(base, universe)
    print(f"[scan] n={len(filled)} ({time.time()-t0:.0f}s)", flush=True)

    pnls = [r["avg_pnl_pct"] for r in filled]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]

    def seg_stats(rows, tag):
        if not rows:
            return
        ps = [r["avg_pnl_pct"] for r in rows]
        hold = [r.get("holding_bars") or 0 for r in rows]
        print(f"[{tag}] n={len(rows)} avg={sum(ps)/len(ps):+.2f}% 中位="
              f"{sorted(ps)[len(ps)//2]:+.2f}% 胜率={sum(1 for p in ps if p>0)/len(ps):.1%} "
              f"avg持有={sum(hold)/len(hold):.1f}日 "
              f"均赢={sum(wins)/len(wins) if wins else 0:.2f}%" if False else "", flush=True)
        print(f"[{tag}] n={len(rows)} avg={sum(ps)/len(ps):+.2f}% 中位="
              f"{sorted(ps)[len(ps)//2]:+.2f}% "
              f"胜率={sum(1 for p in ps if p > 0)/len(ps):.1%} "
              f"avg持有={sum(hold)/len(hold):.1f}日", flush=True)

    seg_stats(filled, "全历史")
    for y in (2022, 2023, 2024, 2025, 2026):
        seg_stats([r for r in filled if str(r["signal_date"])[:4] == str(y)], f"  {y}")

    outer = [r for r in filled if split.outer.covers(pd.Timestamp(r["signal_date"]))]
    seg_stats(outer, f"outer({split.outer.start}~{split.outer.end})")
    m = portfolio_metrics(outer, split.outer, udates)
    n_days = m["n_days"]
    eq = m["equity_end"]
    ann = eq ** (252.0 / n_days) - 1 if n_days > 0 and eq > 0 else 0.0
    print(f"\n[组合口径拆解] outer 交易日 n_days={n_days} 净值倍数 equity_end={eq:.3f}"
          f" → 年化 ann=eq^(252/n_days)-1={ann:+.1%}", flush=True)
    print(f"[仓位口径] PositionModel pos_cap=0.05（每笔5%仓位）× "
          f"avg单笔{sum(r['avg_pnl_pct'] for r in outer)/len(outer):+.2f}%"
          f" → 单笔对组合的期望贡献 ≈ "
          f"{0.05 * sum(r['avg_pnl_pct'] for r in outer)/len(outer):+.3f}%", flush=True)

    json.dump({"n_total": len(filled),
               "avg_all": sum(pnls)/len(pnls),
               "outer": {"n": len(outer), "equity_end": eq, "n_days": n_days,
                          "ann": ann,
                          "avg": sum(r["avg_pnl_pct"] for r in outer)/len(outer)}},
              open("logs/r6_10_per_trade.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_10_per_trade.json", flush=True)


if __name__ == "__main__":
    main()
