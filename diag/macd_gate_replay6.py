# -*- coding: utf-8 -*-
"""MACD 轮引擎收口:弃「死叉态」信号(DIF<DEA 时的颈线突破)。

第七轮唯一温和可测形状:死叉态 六年+0.85%(全条件最弱,outer+0.12),
砍 ~20-23% 笔数。预判按 v2-v5 惯例死(宽度法则),跑实锤。
闸=precompute 预算 DIF/DEA/cross_age,scan_at 前置判。
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


class DropDeathCross(NecklineMethodStrategy):
    def precompute(self, symbol, full_df):
        s = super().precompute(symbol, full_df)
        c = full_df["close"]
        dif = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        dea = dif.ewm(span=9, adjust=False).mean()
        s["death_full"] = dif <= dea
        return s

    def scan_at(self, symbol, df_T, T, strategy_state):
        pos = strategy_state["full_df"].index.get_loc(T)
        if bool(strategy_state["death_full"].iloc[pos]):
            return []
        return super().scan_at(symbol, df_T, T, strategy_state)


def stats(rep):
    pn = [t["avg_pnl_pct"] for t in rep.trades]
    return (f"n={len(pn):>6} 胜率{(sum(1 for x in pn if x>0)/len(pn))*100:>5.1f}% "
            f"均笔{st.mean(pn):+6.3f}% avg_rr={rep.avg_rr:+.4f} "
            f"ann={rep.annualized_return*100:+7.1f}% dd={rep.max_drawdown*100:.1f}%")


params = dict(resolve_champion().params)
frozen, _ = freeze("2021-01-01")
uni = {s: df for s, df in load_universe(start="2020-01-01").items() if s in frozen}
print(f"universe: {len(uni)}", flush=True)

split = holdout_split()
for name in ("inner", "outer"):
    r = split.inner if name == "inner" else split.outer
    for label, S in (("baseline", NecklineMethodStrategy), ("弃死叉态", DropDeathCross)):
        rep = replay(dict(uni), S(cfg_override=params), str(r.start), str(r.end))
        print(f"[{name:>5}][{label:<8}] {stats(rep)}", flush=True)
