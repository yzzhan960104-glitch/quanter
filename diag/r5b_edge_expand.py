# -*- coding: utf-8 -*-
"""R5b 边界外扩：R5-P1 十个单调未收敛维的边界外档位补扫（2026-08-25）。

P1 图谱判定 10 维单维最优落在档位边界（单调未收敛）——单参数调研未完成部分。
本脚本对其外扩档补扫（统一 base=贪心栈终点），产出 logs/r5b_edge_expand.json。
"""
import json
import os
import sys
from copy import deepcopy
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

EXPAND = {
    "tp_h_mult": [0.8, 0.6, 0.5, 0.4],
    "buy_limit_atr_mult": [4.0, 5.0],
    "tp1_h_mult": [2.5, 3.0],
    "min_suppression": [0.3, 0.2],
    "breakout_vol_mult": [0.5],
    "min_rr": [0.5],
    "max_wait": [20, 30],
    "trailing_grace": [25, 30],
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
    r = evaluate_portfolio(p, _W["u"], _W["s"], block_dates=_W["c"])
    print(f"  [{tag:>26}] raw {r['outer_raw']['ann']:+7.1%} | "
          f"inner {r['inner']['ann']:+7.1%} n={r['inner']['n']}", flush=True)
    return tag, r["outer_raw"]["ann"]


def main():
    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    items = []
    for d, lvs in EXPAND.items():
        for lv in lvs:
            p = deepcopy(base); p[d] = lv
            items.append((f"{d}={lv}", p))
    print(f"边界外扩 {len(items)} 项（base +98.4%）", flush=True)
    with Pool(5, initializer=_init) as pool:
        res = list(pool.imap_unordered(_ev, items))
    res.sort(key=lambda t: -t[1])
    print("=== 外扩档排名 ===", flush=True)
    for tag, raw in res:
        print(f"  {tag:>26}: {raw:+.1%}", flush=True)
    json.dump({"base_raw": 0.984, "results": [{"tag": t, "raw": r} for t, r in res]},
              open("logs/r5b_edge_expand.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("[done]", flush=True)


if __name__ == "__main__":
    main()
