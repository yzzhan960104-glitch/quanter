# -*- coding: utf-8 -*-
"""大盘节流 硬闸模拟(2026-09-05 设计取证):block_dates=指数<MA60 日集。

臂:baseline / cyb(创业板指) / dual(双创合成) / hs300(对照) /
cyb_deep(<0.97×MA60,软节流代理)。窗:inner 2025 / outer 2026。
日历口径:信号日 T 的指数收盘 vs 当日 MA60(与 scan_at 决策时点同源,实盘
=T-1 晚计划时点可用)。
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
def d(s): return idx.xs(s, level="symbol")["close"].sort_index()
S = {"cyb": d("399006.SZ"), "hs300": d("000300.SH")}
_r = pd.DataFrame({"cyb": S["cyb"].pct_change(),
                   "kc50": d("000688.SH").pct_change()}).dropna()
S["dual"] = (1 + _r.mean(axis=1)).cumprod()

def calendar(name, mult=1.0):
    s = S[name]
    blocked = s < s.rolling(60).mean() * mult
    return frozenset(blocked[blocked].index.date), blocked.mean()

params = dict(resolve_champion().params)
frozen, _ = freeze("2021-01-01")
uni = {s: df for s, df in load_universe(start="2020-01-01").items() if s in frozen}
print(f"universe: {len(uni)} syms", flush=True)

ARMS = [("baseline", None), ("cyb<MA60", ("cyb", 1.0)), ("dual<MA60", ("dual", 1.0)),
        ("hs300<MA60", ("hs300", 1.0)), ("cyb<0.97MA60", ("cyb", 0.97))]
cal_cache = {}
def stats(rep):
    pn = [t["avg_pnl_pct"] for t in rep.trades]
    return (f"n={len(pn):>6} 胜率{(sum(1 for x in pn if x>0)/len(pn))*100:>5.1f}% "
            f"均笔{st.mean(pn):+6.3f}% avg_rr={rep.avg_rr:+.4f} "
            f"ann={rep.annualized_return*100:+7.1f}% dd={rep.max_drawdown*100:.1f}%")

split = holdout_split()
for name in ("inner", "outer"):
    r = split.inner if name == "inner" else split.outer
    for label, spec in ARMS:
        bd = None
        if spec:
            key = (spec[0], spec[1], name)
            if key not in cal_cache:
                cal_cache[key] = calendar(spec[0], spec[1])
            bd, cover = cal_cache[key]
        rep = replay(dict(uni), NecklineMethodStrategy(cfg_override=params),
                     str(r.start), str(r.end), block_dates=bd)
        cov = f" 拦{cover*100:.0f}%交易日" if bd else ""
        print(f"[{name:>5}][{label:<12}] {stats(rep)}{cov}", flush=True)
