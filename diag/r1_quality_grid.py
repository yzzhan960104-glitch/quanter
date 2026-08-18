# -*- coding: utf-8 -*-
"""R1-2 实证：信号质量方向网格——以 3e383d 为基线沿「少而精」收紧维度探索。

物理意图（R0 结论驱动）：R0 实证组合约束（replay）口径下唯一正边缘是 3e383d
（min_rr 2.0→1.5 + max_h_atr 5.0→2.5，信号 518→67，replay outer +5.5%/dd 1.4%）——
「信号质量优先于数量」是组合约束下唯一有证据的方向。本网格沿该方向系统化：
    ① min_rr × max_h_atr 3×3（核心二维，3e383d 居中）；
    ② 其余质量闸单维探测（breakout_vol_mult/min_touches/min_suppression/window）。

信息隔离（协议 §二）：**选择只看 inner（2025）**，outer（2026）只进报告不反馈选择。
口径：evaluate_replay（replay 引擎=实盘同源，放行口径），默认滑点 5bps。

用法（后台跑，14 组 × ~3.5min ≈ 50min）：
    PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -u diag/r1_quality_grid.py
"""
import sys, os, time, json, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from discovery.snapshot import freeze
from discovery.split import holdout_split
from discovery.objective import evaluate_replay

# ── 基线：3e383d（R0 唯一 replay 正边缘证据链）+ ACTIVE 参照 ──
conn = sqlite3.connect("experiment/experiments.db")
conn.row_factory = sqlite3.Row
BASE = json.loads(conn.execute(
    "SELECT params FROM experiment_version WHERE experiment_id='neckline_prop_20260816_3e383d'"
).fetchone()["params"])
ACTIVE = json.loads(conn.execute(
    "SELECT params FROM experiment_version WHERE status='ACTIVE' ORDER BY weight DESC LIMIT 1"
).fetchone()["params"])

# 网格：核心二维（3e383d 的 min_rr=1.5/max_h_atr=2.5 居中）+ 质量闸单维
GRID = []
for mr in (1.2, 1.5, 1.8):
    for mh in (2.0, 2.5, 3.0):
        GRID.append((f"mr{mr}_mh{mh}", {"min_rr": mr, "max_h_atr": mh}))
GRID += [
    ("vol2.0",   {"breakout_vol_mult": 2.0}),    # 突破带量加倍（放量确认）
    ("touch3",   {"min_touches": 3}),            # 颈线三触（更严形态）
    ("supp0.7",  {"min_suppression": 0.7}),      # 压制时长更长
    ("win60",    {"window": 60}),                # 短窗（更近期形态）
]

t0 = time.time()
universe, meta = freeze("2021-01-01")
split = holdout_split()
print(f"[freeze] universe={meta.universe_count} hash={meta.snapshot_hash} "
      f"用 {time.time()-t0:.0f}s\n", flush=True)

# 基线 3e383d 自身在同口径重跑（网格内参照锚，防跨批次漂移）
results = {}
for label, override in [("*base_3e383d", {})] + GRID:
    params = {**BASE, **override}
    t1 = time.time()
    res = evaluate_replay(params, universe, split)   # 默认滑点 5bps（实盘同源）
    results[label] = {"override": override,
                      "inner": res["inner"], "outer": res["outer"]}
    i, o = res["inner"], res["outer"]
    print(f"[{label:>14}] inner: n={i['n_hits']:>3} ann={i['annualized_return']:+7.1%} "
          f"dd={i['max_drawdown']:6.1%} wr={i['win_rate']:5.1%} | "
          f"outer: n={o['n_hits']:>3} ann={o['annualized_return']:+7.1%} "
          f"dd={o['max_drawdown']:6.1%} wr={o['win_rate']:5.1%} | 用 {time.time()-t1:.0f}s",
          flush=True)

# ── 判定（信息隔离：inner 排序，outer 只报告）──
print("\n=== R1-2 判定（按 inner ann 排序；outer 仅报告不反馈选择）===", flush=True)
try:
    from discovery.fingerprint import engine_hash
    print(f"engine_hash={engine_hash()}", flush=True)
except Exception:
    pass
ranked = sorted(results.items(),
                key=lambda kv: kv[1]["inner"]["annualized_return"], reverse=True)
for label, r in ranked:
    i, o = r["inner"], r["outer"]
    n_ok = "✓" if i["n_hits"] >= 30 else "✗n<30"
    print(f"  {label:>14}: inner ann {i['annualized_return']:+7.1%} (n={i['n_hits']}, {n_ok})"
          f" | outer ann {o['annualized_return']:+7.1%} dd {o['max_drawdown']:6.1%}",
          flush=True)

out_path = os.path.join("logs", "r1_quality_grid_results.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=1, default=str)
print(f"\n[done] 原始数字 → {out_path}", flush=True)
