# -*- coding: utf-8 -*-
"""R5b-T1 修复后关键读数重算（2026-08-25）：T+1 红线对 R4/R5 全部结论的影响面。"""
import json
import os
import sys
from copy import deepcopy
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

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
    print(f"  [{tag:>26}] raw {r['outer_raw']['ann']:+7.1%} | mr {r['outer']['ann']:+7.1%}"
          f" | inner {r['inner']['ann']:+7.1%} n={r['inner']['n']}", flush=True)
    return tag, r["outer_raw"]["ann"], r["outer"]["ann"]


def main():
    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    import sqlite3
    ec = sqlite3.connect("experiment/experiments.db")
    active = json.loads(ec.execute(
        "SELECT params FROM experiment_version WHERE status='ACTIVE' LIMIT 1").fetchone()[0])
    items = [
        ("ACTIVE_25c602", active),
        ("greedy_base", deepcopy(base)),
        ("supp04", {**deepcopy(base), "min_suppression": 0.4}),
    ]
    for tph in (1.5, 1.3, 1.2, 1.1, 1.0, 0.9, 0.8):
        items.append((f"tp_h={tph}", {**deepcopy(base), "tp_h_mult": tph}))
    with Pool(5, initializer=_init) as pool:
        res = list(pool.imap_unordered(_ev, items))
    res.sort(key=lambda t: -t[1])
    print("=== T+1 修复后排名 ===", flush=True)
    for tag, raw, mr in res:
        print(f"  {tag:>22}: raw {raw:+.1%} | 模拟线 {mr:+.1%}", flush=True)
    json.dump([{"tag": t, "raw": r, "mr": m} for t, r, m in res],
              open("logs/r5b_t1_recheck.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("[done]", flush=True)


if __name__ == "__main__":
    main()
