# -*- coding: utf-8 -*-
"""MA 结构轮组合口径复核(v4):弃「颈线低于整条均线带」信号。

第三轮唯一过滤器形态幸存候选:颈线 < min(MA20,50,120,200) 双窗稳定负
(inner -0.03pp n=520 / outer -1.89pp n=807),笔数代价仅 4.7%/6.9%。
闸=后置过滤(super().scan_at 返回后按 sig.neckline 判),
MA 序列 precompute 预算(暖机 NaN=放行)。v2/v3 同款 A/B 底座。
"""
from __future__ import annotations
import statistics as st
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest.replay import replay
from discovery.snapshot import freeze, load_universe
from discovery.split import holdout_split
from experiment.resolver import resolve_champion
from strategies.neckline.strategy import NecklineMethodStrategy

MAS = (20, 50, 120, 200)


class DropNeckBelowRibbon(NecklineMethodStrategy):
    def precompute(self, symbol, full_df):
        s = super().precompute(symbol, full_df)
        rib_min = None
        import pandas as pd
        for m in MAS:
            ma = full_df["close"].rolling(m, min_periods=m).mean()
            rib_min = ma if rib_min is None else pd.concat([rib_min, ma], axis=1).min(axis=1)
        s["rib_min_full"] = rib_min
        return s

    def scan_at(self, symbol, df_T, T, strategy_state):
        sigs = super().scan_at(symbol, df_T, T, strategy_state)
        if not sigs:
            return []
        pos = strategy_state["full_df"].index.get_loc(T)
        rmin = strategy_state["rib_min_full"].iloc[pos]
        if rmin != rmin:                      # NaN 暖机 → 放行(不误杀)
            return sigs
        return [s for s in sigs if not (s.neckline is not None and s.neckline < rmin)]


def stats(rep):
    pn = [t["avg_pnl_pct"] for t in rep.trades]
    return (f"n={len(pn):>6} 胜率{(sum(1 for x in pn if x>0)/len(pn))*100:>5.1f}% "
            f"均笔{st.mean(pn):+6.3f}% avg_rr={rep.avg_rr:+.4f} "
            f"ann={rep.annualized_return*100:+7.1f}% dd={rep.max_drawdown*100:.1f}%")


params = dict(resolve_champion().params)
frozen, _ = freeze("2025-01-01")
uni24 = {s: df for s, df in load_universe(start="2024-01-01").items() if s in frozen}
print(f"extended universe: {len(uni24)} syms", flush=True)

split = holdout_split()
for name in ("inner", "outer"):
    r = split.inner if name == "inner" else split.outer
    for label, S in (("baseline", NecklineMethodStrategy), ("弃颈线带下", DropNeckBelowRibbon)):
        rep = replay(dict(uni24), S(cfg_override=params), str(r.start), str(r.end))
        print(f"[{name:>5}][{label:<9}] {stats(rep)}", flush=True)
