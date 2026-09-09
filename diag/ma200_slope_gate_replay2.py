# -*- coding: utf-8 -*-
"""MA200 斜率闸 组合口径复核 v2(暖机修正版)。

v1 教训:freeze universe 自 2025-01-01 起,inner 窗 MA200 暖机不足(NaN>NaN=False
被误当闸值),1~10月全灭(1814笔残渣)。修正:同符号集加载 2024-01 起扩展历史,
基线/闸臂同数据 A/B(与官方 freeze 数可能微差——以同数据相对比较为准)。
NaN 语义:暖机不足=放行(数据短不误杀,与 momentum_gate 中性原则一致)。
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


class Ma200SlopeGated(NecklineMethodStrategy):
    def precompute(self, symbol, full_df):
        s = super().precompute(symbol, full_df)
        ma = full_df["close"].rolling(200, min_periods=200).mean()
        up = ma > ma.shift(10)
        s["ma200_pass_full"] = up.where(ma.notna(), True)   # NaN→放行
        return s

    def scan_at(self, symbol, df_T, T, strategy_state):
        pos = strategy_state["full_df"].index.get_loc(T)
        if not bool(strategy_state["ma200_pass_full"].iloc[pos]):
            return []
        return super().scan_at(symbol, df_T, T, strategy_state)


def stats(rep):
    pn = [t["avg_pnl_pct"] for t in rep.trades]
    return (f"n={len(pn):>6} 胜率{(sum(1 for x in pn if x>0)/len(pn))*100:>5.1f}% "
            f"均笔{st.mean(pn):+6.3f}% avg_rr={rep.avg_rr:+.4f} "
            f"ann={rep.annualized_return*100:+7.1f}% dd={rep.max_drawdown*100:.1f}%")


params = dict(resolve_champion().params)
frozen, _ = freeze("2025-01-01")            # 官方冻结符号集(口径锚)
uni24 = {s: df for s, df in load_universe(start="2024-01-01").items() if s in frozen}
d0 = min(df.index.min() for df in uni24.values())
d1 = max(df.index.max() for df in uni24.values())
print(f"extended universe: {len(uni24)} syms, {d0.date()}~{d1.date()}", flush=True)

split = holdout_split()
for name in ("inner", "outer"):
    r = split.inner if name == "inner" else split.outer
    for label, S in (("baseline", NecklineMethodStrategy), ("ma200slope", Ma200SlopeGated)):
        rep = replay(dict(uni24), S(cfg_override=params), str(r.start), str(r.end))
        print(f"[{name:>5}][{label:<9}] {stats(rep)}", flush=True)
