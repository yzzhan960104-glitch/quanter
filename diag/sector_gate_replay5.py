# -*- coding: utf-8 -*-
"""板块强度闸 v5 组合口径:弃「20日板块强度最弱四分位」信号。

第四轮(板块维度)唯一可测形状:sec_r20<0.25 弃(宽度代价~10%,
弱市年逐笔省 +1.3~2.3pp,牛年反向 -0.5pp)。构造:门类映射+行业等权
横截面分位在构造器预算,precompute 落该 symbol 的分位序列,scan_at O(1) 查。
NaN(缺行业/缺分位)→放行。v2-v4 同款 A/B 底座。
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

_ind = pd.read_parquet("data_lake/stock_basic.parquet").set_index("ts_code")["industry"]
_rank = pd.read_parquet("diag/sector_rank20.parquet")     # index=date, cols=industry


class DropWeakSector(NecklineMethodStrategy):
    def __init__(self, cfg_override=None, **kw):
        super().__init__(cfg_override, **kw)
        self._industry = None

    def precompute(self, symbol, full_df):
        s = super().precompute(symbol, full_df)
        ind = _ind.get(symbol)
        if ind is not None and ind in _rank.columns:
            s["sec_rank_full"] = _rank[ind].reindex(full_df.index)
        else:
            s["sec_rank_full"] = None
        return s

    def scan_at(self, symbol, df_T, T, strategy_state):
        r = strategy_state.get("sec_rank_full")
        if r is not None:
            pos = strategy_state["full_df"].index.get_loc(T)
            v = r.iloc[pos]
            if v == v and float(v) < 0.25:      # NaN→放行
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
print(f"universe: {len(uni)} syms", flush=True)

split = holdout_split()
for name in ("inner", "outer"):
    r = split.inner if name == "inner" else split.outer
    for label, S in (("baseline", NecklineMethodStrategy), ("弃弱板块Q1", DropWeakSector)):
        rep = replay(dict(uni), S(cfg_override=params), str(r.start), str(r.end))
        print(f"[{name:>5}][{label:<9}] {stats(rep)}", flush=True)
