# -*- coding: utf-8 -*-
"""R6-4 幽灵成交修复验证（2026-08-26 用户裁决「修复」）。

Phase A（等价复测）：R6-1 Phase B 在修复前引擎上实测 tp1_portion=0 ≠ tp1_h_mult=5.0
（6590/18445 笔污染，幽灵成交）；修复（lot1 按当根真实到达价位记账）后两者应
逐位一致——「关闭一档减仓」的两个语义载体合一，这是修复正确性的端到端实证。

Phase B（真值刷新）：tp1_h_mult 2.0+ 污染档重扫（修复后引擎），与 R6a 图谱受染
读数并排对照——幽灵成分的量级一目了然。

产物：logs/r6_4_fix_validation.json。
"""
import json
import os
import sys
import time
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.objective import evaluate_portfolio, run_full_scan

    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    t0 = time.time()
    universe, meta = freeze("2021-01-01")
    split = holdout_split()
    print(f"[init] freeze {len(universe)} syms in {time.time()-t0:.0f}s", flush=True)

    # —— Phase A：等价复测（应转逐位一致）——
    pa = {**deepcopy(base), "tp1_portion": 0.0}
    pb = {**deepcopy(base), "tp1_h_mult": 5.0}
    fa = run_full_scan(pa, universe)
    fb = run_full_scan(pb, universe)

    def _key(r):
        return (r["symbol"], str(r["signal_date"]), r["exit_reason"], r["avg_pnl_pct"])

    ka, kb = [_key(r) for r in fa], [_key(r) for r in fb]
    trades_equal = ka == kb
    n_diff = sum(1 for x, y in zip(ka, kb) if x != y) + abs(len(ka) - len(kb))
    ra = evaluate_portfolio(pa, universe, split)
    rb = evaluate_portfolio(pb, universe, split)
    metrics_equal = ra == rb
    print(f"[A] 逐笔逐位一致: {trades_equal}（n={len(ka)}/{len(kb)}，diff={n_diff}）",
          flush=True)
    print(f"[A] 全指标逐位一致: {metrics_equal} | outer_raw {ra['outer']['ann']:+.1%}",
          flush=True)
    if not metrics_equal:
        for seg in ("inner", "outer"):
            for k in ra[seg]:
                if ra[seg][k] != rb[seg].get(k):
                    print(f"    [diff] {seg}.{k}: {ra[seg][k]!r} vs {rb[seg].get(k)!r}",
                          flush=True)

    # —— Phase B：tp1_h_mult 污染档真值刷新（vs R6a 受染读数并排）——
    try:
        r6a = json.load(open("logs/r6a_rescan.json", encoding="utf-8"))
        r6a_rows = {str(r["lv"]): r["raw"]
                    for r in r6a["dims"]["tp1_h_mult"]["rows"]}
    except Exception:
        r6a_rows = {}
    levels = [2.0, 2.5, 3.0, 5.0]
    print("\n=== tp1_h_mult 污染档真值刷新（R6a 受染 → 修复后真值）===", flush=True)
    refresh = []
    for lv in levels:
        p = {**deepcopy(base), "tp1_h_mult": lv}
        t1 = time.time()
        r = evaluate_portfolio(p, universe, split)
        row = {"lv": lv, "raw_new": r["outer"]["ann"], "n": r["n_total"],
               "sec": round(time.time() - t1, 1)}
        old = r6a_rows.get(str(lv))
        if old is not None:
            row["raw_r6a"] = old
        refresh.append(row)
        old_s = f"{old:+.0%}(R6a)" if old is not None else "n/a"
        print(f"  tp1_h_mult={lv}: {row['raw_new']:+.1%}（修复后） vs {old_s}"
              f" | n={row['n']} {row['sec']}s", flush=True)

    out = {"trades_equal": trades_equal, "n_trades": [len(ka), len(kb)],
           "n_diff": n_diff, "metrics_equal": metrics_equal,
           "outer_raw_p0": ra["outer"]["ann"], "refresh": refresh}
    json.dump(out, open("logs/r6_4_fix_validation.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_4_fix_validation.json", flush=True)


if __name__ == "__main__":
    main()
