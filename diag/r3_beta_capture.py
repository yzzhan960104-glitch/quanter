# -*- coding: utf-8 -*-
"""R3-D3 急涨月 beta 捕获率对照：ACTIVE vs 新候选（2026-08-23）。

用户目标度量：池子月收益 >8% 月份的策略月收益/池子月收益（beta 捕获率）。
对照 ACTIVE（R2 病灶 ~40%）与 R3 新候选（mh4.0/v1.5/tp1.5 系）——L3 追涨腿
立项判据：新候选捕获率 ≥50% 则 L3 缓，<50% 立项。
raw 口径（无模拟线——真实可达表现）；产物 logs/r3_beta_capture.json。
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
import pandas as pd
from discovery.snapshot import freeze
from backtest.replay import replay
from strategies.neckline.strategy import NecklineMethodStrategy
from discovery.manual_risk_sim import _pool_equity

con_params = {
    "active_25c602": json.loads(__import__('sqlite3').connect('experiment/experiments.db').execute(
        "SELECT params FROM experiment_version WHERE status='ACTIVE' LIMIT 1").fetchone()[0]),
}
tcon = __import__('sqlite3').connect('file:logs/discovery_trials.db?mode=ro', uri=True)
con_params["r3_49e3755"] = json.loads(tcon.execute(
    "SELECT params FROM trial WHERE trial_id='49e3755b322c'").fetchone()[0])

universe, meta = freeze("2021-01-01")
pool = _pool_equity(universe)
pool_m = pool[pool.index >= '2026-01-01'].resample('ME').last().pct_change()

out = {"pool_monthly": {str(k.date()): round(v, 4) for k, v in pool_m.items()},
       "capture": {}, "surge_months": [str(k.date()) for k, v in pool_m.items() if v > 0.08]}
for name, params in con_params.items():
    t0 = time.time()
    strat = NecklineMethodStrategy(cfg_override=params)
    rep = replay(universe, strat, '2026-01-01', '2026-08-21')
    ec = pd.DataFrame(rep.equity_curve)
    ec['m'] = pd.to_datetime(ec['date']).dt.strftime('%Y-%m')
    strat_m = ec.groupby('m')['equity'].last().pct_change()
    cap = {}
    for m, pool_r in pool_m.items():
        key = f"{m.year:04d}-{m.month:02d}"
        if key in strat_m.index and pd.notna(strat_m[key]) and pool_r > 0.08:
            cap[key] = round(float(strat_m[key]) / float(pool_r), 3)
    out["capture"][name] = {"monthly": {k: round(float(v), 4) for k, v in strat_m.items()},
                            "capture_surge": cap}
    print(f"[{name}] 急涨月捕获率 {cap} | 用 {(time.time()-t0)/60:.0f}min", flush=True)

with open("logs/r3_beta_capture.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=str)
print("[done] logs/r3_beta_capture.json")
