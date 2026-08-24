# -*- coding: utf-8 -*-
"""R6-PhaseA：T+1 修复后引擎上的全参数单维真值重扫（2026-08-25）。

R5-P1 图谱与 R5b 外扩全部基于含 T+1 违规的旧引擎——本脚本在修复后引擎上
一次性重扫（22 维全档 + 边界外扩档，共 ~93 项，统一 base=贪心栈），
产出真值版敏感性图谱 logs/r6a_rescan.json（单参数调研的最终定稿数据）。
"""
import json
import os
import sys
from copy import deepcopy
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# 22 维全档（R5-P1 档位 ∪ R5b 外扩档）
LEVELS = {
    "window": [40, 80, 100, 120],
    "min_touches": [3, 4],
    "min_suppression": [0.3, 0.2, 0.4, 0.6, 0.7, 0.8],
    "local_extrema_window": [5, 7],
    "min_bottoms": [3, 4],
    "breakout_vol_mult": [0.5, 0.8, 1.5, 2.0],
    "min_rr": [0.5, 1.0, 2.0, 2.5, 3.0],
    "max_h_atr": [3.5, 4.0, 5.0, 5.5, 6.0],
    "stop_atr_mult": [1.0, 2.0, 2.5],
    "tp_h_mult": [0.65, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 2.0, 2.5, 3.0],
    "decay_tau": [None, 60, 90, 120],
    "momentum_gate": [0.0, 0.03, 0.05, 0.10],
    "max_holding": [10, 15, 20, 40, 50],
    "max_wait": [3, 8, 12, 20, 30],
    "cooldown": [0, 5, 8],
    "buy_limit_atr_mult": [0.5, 1.0, 2.0, 2.5, 3.0, 4.0, 5.0],
    "tp1_h_mult": [0.5, 1.0, 2.0, 2.5, 3.0],
    "tp1_portion": [0.3, 0.5, 0.9],
    "cancel_thresh_mult": [1.0, 2.0, 3.0],
    "trailing_grace": [0, 5, 15, 20, 25, 30],
    "trailing_step": [0.0, 0.03, 0.1, 0.15],
    "trailing_floor": [0.0, 0.25, 0.75],
}
_W = {}


def _init():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.manual_risk_sim import build_block_calendar
    _W["u"], m = freeze("2021-01-01")
    _W["s"] = holdout_split()
    _W["c"] = build_block_calendar(_W["u"])


def _ev(item):
    if "u" not in _W:
        _init()
    from discovery.objective import evaluate_portfolio
    tag, p = item
    try:
        r = evaluate_portfolio(p, _W["u"], _W["s"], block_dates=_W["c"])
        out = {"tag": tag, "raw": r["outer_raw"]["ann"], "mr": r["outer"]["ann"],
               "inner": r["inner"]["ann"], "n": r["inner"]["n"]}
        print(f"  [{tag:>30}] raw {out['raw']:+7.1%} | n={out['n']}", flush=True)
    except Exception as e:
        out = {"tag": tag, "error": f"{type(e).__name__}: {e}"}
        print(f"  [{tag:>30}] ERR {e}", flush=True)
    return out


def main():
    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    items = [("BASE", deepcopy(base))]
    for dim, levels in LEVELS.items():
        for lv in levels:
            if base.get(dim) == lv:
                continue
            p = deepcopy(base); p[dim] = lv
            items.append((f"{dim}={lv}", p))
    print(f"[R6a] T+1 修复后全参数重扫 {len(items)} 项", flush=True)
    with Pool(6, initializer=_init) as pool:
        res = list(pool.imap_unordered(_ev, items))
    base_raw = next(r["raw"] for r in res if r["tag"] == "BASE")
    import collections
    per = collections.defaultdict(list)
    for r in res:
        if "error" in r or r["tag"] == "BASE":
            continue
        d, lv = r["tag"].split("=", 1)
        try:
            lv = json.loads(lv)
        except Exception:
            pass
        per[d].append({"lv": lv, "raw": r["raw"], "n": r["n"]})
    summary = {"base_raw": base_raw, "dims": {}}
    print(f"\n=== 真值版图谱（base {base_raw:+.1%}）===", flush=True)
    for d, rows in sorted(per.items()):
        best = max(rows, key=lambda x: x["raw"])
        numeric = [x["lv"] for x in rows if isinstance(x["lv"], (int, float))]
        edge = (isinstance(best["lv"], (int, float)) and numeric and
                (best["lv"] == max(numeric) or best["lv"] == min(numeric)) and best["raw"] > base_raw)
        rows_sorted = sorted(rows, key=lambda x: str(x["lv"]))
        print(f"  {d:>22}: " + " ".join(f"{x['lv']}:{x['raw']:+.0%}" for x in rows_sorted) +
              (f"  ★边界未收敛(best={best['lv']})" if edge else ""), flush=True)
        summary["dims"][d] = {"rows": rows, "best_lv": best["lv"], "best_raw": best["raw"],
                              "edge_unconverged": edge}
    json.dump(summary, open("logs/r6a_rescan.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6a_rescan.json", flush=True)


if __name__ == "__main__":
    main()
