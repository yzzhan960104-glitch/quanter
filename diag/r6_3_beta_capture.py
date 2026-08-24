# -*- coding: utf-8 -*-
"""R6-3 L3 追涨腿地基：真值口径 beta 捕获率重测（2026-08-26）。

R3-D3 判据（4 月型急涨捕获 54.2% 过线 / 8 月型 V 反两者皆负 −20%/−11%）全部
基于 **T+1 修复前引擎 + 旧参数系**（ACTIVE/49e3755）。本脚本在修复后引擎上
重测两个帕累托锚（贪心栈 base / base+supp0.4）的月度 beta 捕获率，回答：
① V 反盲区在真值口径下是否仍在、多大；② 急涨月（池子月>+8%）捕获是否过
50% 线——L3 立项判据的真值版。replay 口径（与 r3_beta_capture.py 同源，
rep.equity_curve 月末 pct_change / 池子等权月收益），raw（无模拟线）。

产物：logs/r6_3_beta_capture.json。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd


def main():
    from discovery.snapshot import freeze
    from backtest.replay import replay
    from strategies.neckline.strategy import NecklineMethodStrategy
    from discovery.manual_risk_sim import _pool_equity

    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    cands = {
        "greedy_base": base,
        "supp04": {**base, "min_suppression": 0.4},
    }
    universe, meta = freeze("2021-01-01")
    pool = _pool_equity(universe)
    pool_m = pool[pool.index >= "2025-01-01"].resample("ME").last().pct_change()
    surge = [k for k, v in pool_m.items() if pd.notna(v) and v > 0.08]
    crash = [k for k, v in pool_m.items() if pd.notna(v) and v < -0.10]
    print("[pool] 月度:", " ".join(f"{k.strftime('%Y-%m')}:{v:+.0%}"
                                    for k, v in pool_m.items() if pd.notna(v)), flush=True)
    print(f"[pool] 急涨月(>+8%): {[k.strftime('%Y-%m') for k in surge]} | "
          f"急跌月(<-10%): {[k.strftime('%Y-%m') for k in crash]}", flush=True)

    out = {"pool_monthly": {str(k.date()): round(v, 4) for k, v in pool_m.items()},
           "surge_months": [str(k.date()) for k in surge],
           "crash_months": [str(k.date()) for k in crash], "capture": {}}
    for name, params in cands.items():
        t0 = time.time()
        strat = NecklineMethodStrategy(cfg_override=params)
        rep = replay(universe, strat, "2025-01-01", "2026-08-21")
        ec = pd.DataFrame(rep.equity_curve)
        ec["m"] = pd.to_datetime(ec["date"]).dt.strftime("%Y-%m")
        strat_m = ec.groupby("m")["equity"].last().pct_change()
        cap = {}
        for k, pool_r in pool_m.items():
            key = "%04d-%02d" % (k.year, k.month)
            if key in strat_m.index and pd.notna(strat_m[key]) and pool_r > 0.08:
                cap[key] = round(float(strat_m[key]) / float(pool_r), 3)
        avg_cap = round(sum(cap.values()) / len(cap), 3) if cap else None
        out["capture"][name] = {"monthly": {k: round(float(v), 4)
                                            for k, v in strat_m.items()},
                                "capture_surge": cap, "avg_capture_surge": avg_cap}
        print(f"[{name}] 急涨月捕获 {cap}（均值 {avg_cap}）| "
              f"用 {(time.time()-t0)/60:.0f}min", flush=True)

    json.dump(out, open("logs/r6_3_beta_capture.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_3_beta_capture.json", flush=True)


if __name__ == "__main__":
    main()
