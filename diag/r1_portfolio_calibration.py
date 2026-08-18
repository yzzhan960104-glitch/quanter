# -*- coding: utf-8 -*-
"""R1-1 标定：evaluate_portfolio（组合口径后处理）vs evaluate_replay（replay 引擎）。

物理意图：搜索目标切组合口径的前提是「便宜版 ≈ 引擎版」。本脚本对 R0 三个参数集
（ACTIVE 25c602 / 已弃 47d350 / R1 种子 3e383d——覆盖 replay outer 负/深负/正三种
形态）双口径对比 inner/outer 的 ann 与 dd：
    · 符号一致 + 幅度同量级 → 搜索可切 evaluate_portfolio（快 60-100 倍）；
    · 符号反转 → 不可切，搜索必须直接跑 evaluate_replay（本脚本即否决证据）。

已知边界差异（evaluate_portfolio docstring 列举）：分段口径/段末持仓/完整性 gate——
标定数字即这些差异的净效应量化。

用法：PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -u diag/r1_portfolio_calibration.py
"""
import sys, os, time, json, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from discovery.snapshot import freeze
from discovery.split import holdout_split
from discovery.objective import evaluate_replay, evaluate_portfolio

conn = sqlite3.connect("experiment/experiments.db")
conn.row_factory = sqlite3.Row
PARAM_SETS = {}
for eid in ("neckline_disc_20260725_25c602", "neckline_prop_20260816_47d350",
            "neckline_prop_20260816_3e383d"):
    row = conn.execute("SELECT params FROM experiment_version WHERE experiment_id=?",
                       (eid,)).fetchone()
    if row is not None:
        PARAM_SETS[eid.split("_")[-1]] = json.loads(row["params"])

t0 = time.time()
universe, meta = freeze("2021-01-01")
split = holdout_split()
print(f"[freeze] universe={meta.universe_count} hash={meta.snapshot_hash} "
      f"用 {time.time()-t0:.0f}s\n", flush=True)

out = {}
agree = True
for label, params in PARAM_SETS.items():
    t1 = time.time()
    rep = evaluate_replay(params, universe, split)          # 引擎口径（贵，放行判据）
    t2 = time.time()
    port = evaluate_portfolio(params, universe, split)      # 组合后处理口径（便宜，候选搜索口径）
    print(f"[{label}] 引擎 {t2-t1:.0f}s vs 后处理 {time.time()-t2:.0f}s", flush=True)
    for seg in ("inner", "outer"):
        r, p = rep[seg], port[seg]
        sign_ok = (r["annualized_return"] > 0) == (p["ann"] > 0)
        agree = agree and sign_ok
        print(f"  {seg:5}: 引擎 ann {r['annualized_return']:+7.2%} dd {r['max_drawdown']:7.2%} "
              f"wr {r['win_rate']:5.1%} n={r['n_hits']:>3} | 后处理 ann {p['ann']:+7.2%} "
              f"dd {p['max_dd']:7.2%} wr {p['win_rate']:5.1%} n={p['n']:>3}"
              f"/入净值{p['n_taken']:>3} | 符号一致={'✓' if sign_ok else '✗'}", flush=True)
    out[label] = {"replay": {s: rep[s] for s in ("inner", "outer")},
                  "portfolio": {s: {k: v for k, v in port[s].items()
                                    if k != "yearly_calmar"} for s in ("inner", "outer")}}
    print(flush=True)

print(f"=== 标定判定：符号全一致={'是' if agree else '否'} ===", flush=True)
try:
    from discovery.fingerprint import engine_hash
    print(f"engine_hash={engine_hash()}", flush=True)
except Exception:
    pass
with open(os.path.join("logs", "r1_portfolio_calibration.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=str)
print("[done] → logs/r1_portfolio_calibration.json", flush=True)
