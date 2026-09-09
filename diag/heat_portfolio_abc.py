# -*- coding: utf-8 -*-
"""过热度维度 组合口径三形态复核(24h 审计 · 步骤C · 2026-09-05)。

因子审计唯一三关幸存维度=过热度(turnover/turnover20/idio/gap20 共线簇,
六年 Q1冷 +1.39% vs Q5热 +0.32%)。三种用法同一语料、同一部署口径 PM
(4×7.5% 帽,random 21 种子中位数 + oracle 对照)逐位比较:
  A 过滤:弃最热五分位(宽度 -20%)
  B 排序:同日候选按「冷度」优先进场(queue_order=priority,零宽度损失)
  C 弹性仓位:冷 10% / 中 7.5% / 热 3%(quality_alloc,零宽度损失)
分年(2021-2026)逐年报,防单窗口幻觉。
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd

from backtest.models import PositionModel
from discovery.objective import portfolio_metrics
from discovery.split import Segment

Z = pd.read_parquet("diag/factor_zoo_heat.parquet")
tr = pd.read_parquet("diag/neckline_regime_trades.parquet").loc[Z.index]
# 热度分位(全期)→ 冷度 = 1 − heat_g;当日横截面分位更贴实盘(top5 同日选序)
Z["cold"] = 1.0 - Z["heat_g"]
Z["cold_day"] = 1.0 - Z.groupby("formed_at")["heat_g"].rank(pct=True)   # 同日内冷度分位
Z["hq_n"] = pd.qcut(Z["heat_g"], 5, labels=False)                      # 0 冷 … 4 热

def rows(mask, priority=None, pos_cap=None):
    sub = tr.loc[Z.index[mask]]
    pr = None if priority is None else priority.loc[sub.index].astype(float).values
    pc = None if pos_cap is None else pos_cap.loc[sub.index].astype(float).values
    sd = pd.to_datetime(sub["formed_at"]).values
    return [{"symbol": s, "signal_date": pd.Timestamp(d), "buy_date": bd, "exit_date": ed,
             "avg_pnl_pct": float(p), "entry": None,
             "priority": None if pr is None else float(pr[i]),
             "pos_cap": None if pc is None else float(pc[i])}
            for i, (s, d, bd, ed, p) in enumerate(zip(
                sub["symbol"].values, sd, sub["entry_date"].values,
                sub["exit_date"].values, sub["avg_pnl_pct"].values))]

cal = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"]).xs(
    "000300.SH", level="symbol").index.sort_values()
# 部署口径 PM:4×7.5% 帽 + 冻结挂单;语料无 entry_price → 整手/min5 关(lot_size=0)
PM = PositionModel(capital=200_000.0, pos_cap=0.075, max_positions=4,
                   lot_size=0, min_fee=0.0, freeze_pending=True)

all_mask = pd.Series(True, index=Z.index)
cap_map = Z["hq_n"].map({0: 0.10, 1: 0.10, 2: 0.075, 3: 0.075, 4: 0.03})

ARMS = {
    "基线(等权 7.5%)": dict(mask=all_mask),
    "A 过滤:弃最热Q5": dict(mask=Z["hq_n"] < 4),
    "B 排序:冷度优先": dict(mask=all_mask, priority=Z["cold_day"]),
    "C 弹性仓位 10/7.5/3": dict(mask=all_mask, pos_cap=cap_map),
}

def run(arm, seg):
    trades = rows(arm["mask"], arm.get("priority"), arm.get("pos_cap"))
    pm = PM
    if arm.get("priority") is not None:
        pm = replace(pm, queue_order="priority")
    if arm.get("pos_cap") is not None:
        pm = replace(pm, quality_alloc=True)
    if arm.get("priority") is not None:
        # 排序臂无随机对照(确定性序);报 oracle 与 priority 双值
        pr = portfolio_metrics(trades, seg, cal, position_model=pm)
        orc = portfolio_metrics(trades, seg, cal, position_model=replace(pm, queue_order="exit_date"))
        return pr, orc, None
    orc = portfolio_metrics(trades, seg, cal, position_model=pm)
    seeds = [portfolio_metrics(trades, seg, cal,
                               position_model=replace(pm, queue_order="random", queue_seed=s))
             for s in range(21)]
    dep = {k: float(np.median([m[k] for m in seeds])) for k in ("ann", "max_dd", "calmar", "n_taken", "win_rate")}
    return dep, orc, seeds

for y in range(2021, 2027):
    seg = Segment(f"y{y}", pd.Timestamp(f"{y}-01-01").date(), pd.Timestamp(f"{y}-12-31").date())
    print(f"\n==== {y} ====")
    print(f"{'臂':<20}{'部署ann':>9}{'部署dd':>8}{'calmar':>8}{'n_taken':>8}{'胜率':>7} | {'oracle ann':>10}")
    for name, arm in ARMS.items():
        dep, orc, _ = run(arm, seg)
        tag = "(priority序)" if arm.get("priority") is not None else ""
        print(f"{name:<20}{dep['ann']*100:>+8.1f}%{dep['max_dd']*100:>+7.1f}%{dep['calmar']:>8.2f}"
              f"{dep['n_taken']:>8.0f}{dep['win_rate']*100:>6.1f}% | {orc['ann']*100:>+9.1f}% {tag}", flush=True)

print("\n==== 六年全期 2021-01-01~2026-09-04 ====")
seg = Segment("full", pd.Timestamp("2021-01-01").date(), pd.Timestamp("2026-09-04").date())
for name, arm in ARMS.items():
    dep, orc, _ = run(arm, seg)
    print(f"{name:<20} 部署 ann{dep['ann']*100:>+7.1f}% dd{dep['max_dd']*100:>+6.1f}% calmar{dep['calmar']:>6.2f} "
          f"n_taken{dep['n_taken']:>5.0f} 胜率{dep['win_rate']*100:>5.1f}% | oracle ann{orc['ann']*100:>+7.1f}%", flush=True)
