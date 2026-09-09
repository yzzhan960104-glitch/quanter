# -*- coding: utf-8 -*-
"""缠线带节流 引擎验证:block_dates = {0.97×MA60 ≤ 创业板指 < MA60}。

三区结构(六年逐笔):NORMAL +2.10%(62%) / 缠线带 -1.05%(15%,毒药) /
深区 +0.10%(23%,底部反转主场)。预注册候选=只拦缠线带。对照臂=深区也拦。
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

import pandas as pd

from backtest.replay import replay
from discovery.snapshot import freeze, load_universe
from discovery.split import holdout_split
from experiment.resolver import resolve_champion
from strategies.neckline.strategy import NecklineMethodStrategy

idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
cyb = idx.xs("399006.SZ", level="symbol")["close"].sort_index()
ma60 = cyb.rolling(60).mean()
band = (cyb < ma60) & (cyb >= ma60 * 0.97)          # 缠线带
band_cal = frozenset(band[band].index.date)
below = cyb < ma60                                    # 全空头带(对照)
below_cal = frozenset(below[below].index.date)

params = dict(resolve_champion().params)
frozen, _ = freeze("2021-01-01")
uni = {s: df for s, df in load_universe(start="2020-01-01").items() if s in frozen}
print(f"universe: {len(uni)} | 缠线带占 {band.mean()*100:.0f}% 日", flush=True)

def stats(rep):
    pn = [t["avg_pnl_pct"] for t in rep.trades]
    return (f"n={len(pn):>6} 胜率{(sum(1 for x in pn if x>0)/len(pn))*100:>5.1f}% "
            f"均笔{st.mean(pn):+6.3f}% avg_rr={rep.avg_rr:+.4f} "
            f"ann={rep.annualized_return*100:+7.1f}% dd={rep.max_drawdown*100:.1f}%")

split = holdout_split()
for name in ("inner", "outer"):
    r = split.inner if name == "inner" else split.outer
    for label, bd in (("baseline", None), ("只拦缠线带", band_cal), ("拦全空头带", below_cal)):
        rep = replay(dict(uni), NecklineMethodStrategy(cfg_override=params),
                     str(r.start), str(r.end), block_dates=bd)
        print(f"[{name:>5}][{label:<8}] {stats(rep)}", flush=True)
