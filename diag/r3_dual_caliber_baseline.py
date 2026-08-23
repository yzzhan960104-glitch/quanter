# -*- coding: utf-8 -*-
"""R3-D1 五参照双口径基线：raw vs 人工风控模拟线（2026-08-23）。

新旧尺子的对照表——G2/G3 切模拟线口径后，既有候选在新尺子下的读数。
每参照跑 evaluate_replay 两遍（raw / block_dates 同日历），inner+outer 全段。
产物：logs/r3_dual_caliber_baseline.json（进 ROUND_LOG R3 节基线表）。
用法：PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -u diag/r3_dual_caliber_baseline.py
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
from discovery.snapshot import freeze
from discovery.split import holdout_split
from discovery.objective import evaluate_replay
from discovery.manual_risk_sim import build_block_calendar

REFS = ["neckline_disc_20260725_25c602",        # ACTIVE 基线
        "neckline_prop_20260816_3e383d",        # R1 种子
        "neckline_r1_touch3_20260816",          # R1 冠军（注：DRAFT 名以 DB 为准）
        "neckline_disc_20260822_3f6365",        # R2 TPE #1
        "neckline_r2_mh27_t2_w60_s06_v10_20260823"]  # R2 稳健独苗

import sqlite3
con = sqlite3.connect("experiment/experiments.db")
con.row_factory = sqlite3.Row
params_by_id = {}
for eid in REFS:
    rows = list(con.execute(
        "SELECT params, status FROM experiment_version WHERE experiment_id=? ORDER BY version DESC", (eid,)))
    if rows:
        params_by_id[eid] = (json.loads(rows[0]["params"]), rows[0]["status"])
con.close()

universe, meta = freeze("2021-01-01")
split = holdout_split()
cal = build_block_calendar(universe)
print(f"[baseline] universe={meta.universe_count} 日历拦截 {len(cal)} 天", flush=True)

out = {"snapshot_hash": meta.snapshot_hash, "block_days": len(cal), "refs": {}}
for eid, (params, status) in params_by_id.items():
    t0 = time.time()
    raw = evaluate_replay(params, universe, split)
    blk = evaluate_replay(params, universe, split, block_dates=cal)
    out["refs"][eid] = {"status": status,
        "raw": {"inner": raw["inner"], "outer": raw["outer"]},
        "blocked": {"inner": blk["inner"], "outer": blk["outer"]}}
    r_o, b_o = raw["outer"], blk["outer"]
    print(f"[{eid[:36]:>36}] outer ann: raw {r_o['annualized_return']:+7.1%} → 模拟线 "
          f"{b_o['annualized_return']:+7.1%} | dd: {r_o['max_drawdown']:.1%}→{b_o['max_drawdown']:.1%} "
          f"| n {r_o['n_hits']}→{b_o['n_hits']} | 用 {(time.time()-t0)/60:.0f}min", flush=True)

with open("logs/r3_dual_caliber_baseline.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=str)
print("[done] logs/r3_dual_caliber_baseline.json", flush=True)
