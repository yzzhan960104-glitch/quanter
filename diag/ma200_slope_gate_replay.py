# -*- coding: utf-8 -*-
"""MA200 斜率闸 组合口径复核(新分支第 5 步 · 2026-09-05)。

候选唯一幸存条件:MA200 10日斜率向上(双窗均 +1.33pp 逐笔改善)。
闸=研究态子类(precompute 预算斜率序列,无前视:rolling 到 T、对比 T-10);
不动产品代码——组合口径通过后再谈提案流。

口径:与 r68_vs_b3_holdout 同池同窗(inner 2025 / outer 2026),
同引擎 A/B:基线 NecklineMethodStrategy vs Ma200SlopeGated。
注意:replay 权益曲线含 queue 排序口径(先知味),只做同引擎相对比较。
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
from discovery.snapshot import freeze
from discovery.split import holdout_split
from experiment.resolver import resolve_champion
from strategies.neckline.strategy import NecklineMethodStrategy


class Ma200SlopeGated(NecklineMethodStrategy):
    """MA200 10日斜率向下 → 丢弃信号(决策时点 T 可算,无前视)。"""

    def precompute(self, symbol, full_df):
        st_ = super().precompute(symbol, full_df)
        ma = full_df["close"].rolling(200, min_periods=200).mean()
        st_["ma200_up_full"] = (ma > ma.shift(10))
        return st_

    def scan_at(self, symbol, df_T, T, strategy_state):
        pos = strategy_state["full_df"].index.get_loc(T)
        ok = strategy_state["ma200_up_full"].iloc[pos]
        if not bool(ok):                 # NaN(暖机不足)也按 False 处理?——按 True 放行(数据短不误杀,与 momentum_gate 中性原则一致)
            import pandas as pd
            if pd.notna(strategy_state["ma200_up_full"].iloc[pos]):
                return []
        return super().scan_at(symbol, df_T, T, strategy_state)


def stats(rep):
    pn = [t["avg_pnl_pct"] for t in rep.trades]
    return (f"n={len(pn):>6} 胜率{(sum(1 for x in pn if x>0)/len(pn))*100:>5.1f}% "
            f"均笔{st.mean(pn):+6.3f}% avg_rr={rep.avg_rr:+.4f} "
            f"ann={rep.annualized_return*100:+7.1f}% dd={rep.max_drawdown*100:.1f}%")


params = dict(resolve_champion().params)
universe, _ = freeze("2025-01-01")
split = holdout_split()
for name in ("inner", "outer"):
    r = split.inner if name == "inner" else split.outer
    for label, S in (("baseline", NecklineMethodStrategy), ("ma200slope↑", Ma200SlopeGated)):
        rep = replay(universe, S(cfg_override=params), str(r.start), str(r.end))
        print(f"[{name:>5}][{label:<11}] {stats(rep)}", flush=True)
