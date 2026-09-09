# -*- coding: utf-8 -*-
"""MA200 斜率闸 组合口径复核 v3(Q1 分位校准版)。

v2(布尔零阈)双窗年化均降被否;v3=三维度重测后唯一双窗稳定幸存者的最佳校准
(inner Q1 阈值=+0.536%/10日,砍最差四分位,双窗逐笔均 +0.32~0.40pp)。
若此版也降年化,则 MA 家族在过滤口径下彻底穷尽(斜率/宽度/支撑三维)。
扩展暖机+NaN 放行+v2 同款 A/B。
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

THRESH = 0.00536          # inner slp200 Q1:MA200 10日变化 < +0.536% → 弃


class Ma200SlopeQ1(NecklineMethodStrategy):
    def precompute(self, symbol, full_df):
        s = super().precompute(symbol, full_df)
        ma = full_df["close"].rolling(200, min_periods=200).mean()
        slp = ma / ma.shift(10) - 1.0
        s["ma200_ok_full"] = (slp >= THRESH).where(ma.notna(), True)   # NaN→放行
        return s

    def scan_at(self, symbol, df_T, T, strategy_state):
        pos = strategy_state["full_df"].index.get_loc(T)
        if not bool(strategy_state["ma200_ok_full"].iloc[pos]):
            return []
        return super().scan_at(symbol, df_T, T, strategy_state)


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
    for label, S in (("baseline", NecklineMethodStrategy), ("ma200Q1闸", Ma200SlopeQ1)):
        rep = replay(dict(uni24), S(cfg_override=params), str(r.start), str(r.end))
        print(f"[{name:>5}][{label:<9}] {stats(rep)}", flush=True)
