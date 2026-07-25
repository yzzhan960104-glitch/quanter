# -*- coding: utf-8 -*-
"""层4：识别层参数扫描（min_suppression × max_h_atr 网格）。

采样 top100 流动性标的（提速），对每组识别参数跑颈线法，对比胜率/avg，
找最优识别参数组合。输出 logs/identify_param_scan.csv。
"""
import os, sys, itertools
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from strategies.neckline.method_v0 import detect_neckline_method, DEFAULTS, compute_atr
from strategies.neckline.backtest import simulate_exit, dedup_signals

lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
syms = lake.index.get_level_values("symbol").unique().tolist()

print("算流动性选 top100...")
amt = {}
for s in syms:
    try:
        amt[s] = float(lake.xs(s, level="symbol")["amount"].tail(30).mean())
    except Exception:
        amt[s] = 0.0
top = sorted(amt, key=amt.get, reverse=True)[:100]
print(f"top100 采样（提速），参数网格扫描...\n")

# 参数网格：(min_suppression, max_h_atr)
PARAMS = list(itertools.product([0.5, 0.6, 0.7], [3.0, 4.0, 5.0]))
results = []
for ms, mha in PARAMS:
    cfg = {**DEFAULTS, "min_suppression": ms, "max_h_atr": mha}
    filled = []
    for sym in top:
        try:
            sym_df = lake.xs(sym, level="symbol").sort_index()
        except Exception:
            continue
        atr_full = compute_atr(sym_df["high"], sym_df["low"], sym_df["close"])
        signals = []
        for i in range(cfg["window"], len(sym_df)):
            res = detect_neckline_method(sym_df.iloc[: i + 1], cfg, atr_series=atr_full.iloc[: i + 1])
            if res is not None:
                signals.append((i, res))
        signals = dedup_signals(signals)
        for sig_idx, res in signals:
            sim = simulate_exit(sym_df, sig_idx, res["neckline"], res["bottom"], res["atr"])
            if sim and sim["exit_reason"] != "skip_no_pullback":
                filled.append(sim)
    pnls = [r["avg_pnl_pct"] for r in filled]
    n = len(filled)
    wr = sum(1 for p in pnls if p > 0) / n if n else 0
    avg = sum(pnls) / n if n else 0
    sl = sum(1 for r in filled if r["exit_reason"] == "stop_loss") / n if n else 0
    print(f"  min_supp={ms} max_h_atr={mha}: {n:>4}笔 | 胜率 {wr*100:>5.1f}% | avg {avg:+6.2f}% | 止损率 {sl*100:>4.0f}%")
    results.append({"min_supp": ms, "max_h_atr": mha, "n": n, "win_rate": wr, "avg_pnl": avg, "stop_rate": sl})

pd.DataFrame(results).to_csv("logs/identify_param_scan.csv", index=False, encoding="utf-8-sig")
best = max(results, key=lambda x: x["avg_pnl"])
print(f"\n=== 最优（按 avg）: min_supp={best['min_supp']} max_h_atr={best['max_h_atr']} "
      f"avg={best['avg_pnl']:+.2f}% 胜率={best['win_rate']*100:.1f}% ===")
