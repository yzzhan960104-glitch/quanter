# -*- coding: utf-8 -*-
"""重审判委员会(2026-09-06):全部历史否定干预 × 实盘口径 × 五票裁决。

候选=后验可复测的全部干预(参数级 stop_atr_mult 类改变出场结构,无法
后验,不入本轮;ts8 已在 oracle 审计中判 HOLD,汇总引用)。
票制:V1 逐笔稳定(六年非劣+分年≥4/6) V2 实盘PM-B年化非劣(≤1pp)且
(dd改善≥1pp 或 calmar≥基线)【强制】 V3 PM-A 稳健 V4 种子带位移
(ann≥+0.5pp 或 dd≥3pp 且年化带不低于基线中位) V5 机制预注册。
PASS=V2+总票≥4;CONDITIONAL=V2+票=3;FAIL=其余。
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

tr = pd.read_parquet("diag/retrial_corpus.parquet")
tr["formed_at"] = pd.to_datetime(tr["formed_at"])
tr["year"] = tr["formed_at"].dt.year
ma3d = pd.read_parquet("diag/ma3d_trades_tagged.parquet")[["symbol", "formed_at", "slp200"]]
ma3d["formed_at"] = pd.to_datetime(ma3d["formed_at"])
tr = tr.merge(ma3d.drop_duplicates(["symbol", "formed_at"]), on=["symbol", "formed_at"], how="left")

# ── 实盘口径底座:amihud keep-top5 ──
lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2019-06-01"))],
                       columns=["close", "amount"])
lk = lake[lake.index.get_level_values("symbol").isin(set(tr["symbol"].unique()))]
ret = lk["close"].groupby(level="symbol").pct_change()
am = (ret.abs() / (lk["amount"].astype("float32") + 1.0)) \
    .groupby(level="symbol").rolling(60, min_periods=48).mean()
am = am.reset_index(level=0, drop=True).sort_index()
KEY = pd.MultiIndex.from_arrays([tr["formed_at"], tr["symbol"]])
tr["amihud60"] = am.reindex(KEY).values

def keep_top5(g):
    m = pd.Series(True, index=g.index)
    if len(g) <= 5:
        return m
    valid = g.dropna(subset=["amihud60"])
    if len(valid) < 5:
        return m
    keep = valid.sort_values("amihud60", ascending=False).head(5).index
    m.loc[g.index.difference(keep)] = False
    return m
tr["sel"] = tr.groupby("formed_at", group_keys=False).apply(keep_top5)
BASE = tr[tr["sel"]].copy()
print(f"keep-top5 基线 n={len(BASE)} 六年逐笔 {BASE.avg_pnl_pct.mean():+.2f}%", flush=True)

idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
cal = idx.xs("000300.SH", level="symbol").index.sort_values()
PMS = {"B": PositionModel(capital=200_000.0, pos_cap=0.05, max_positions=20,
                          lot_size=0, min_fee=0.0, freeze_pending=True),
       "A": PositionModel(capital=200_000.0, pos_cap=0.075, max_positions=4,
                          lot_size=0, min_fee=0.0, freeze_pending=True)}
full = Segment("full", pd.Timestamp("2021-01-01").date(), pd.Timestamp("2026-09-04").date())
years = {y: Segment(f"y{y}", pd.Timestamp(f"{y}-01-01").date(), pd.Timestamp(f"{y}-12-31").date())
         for y in range(2021, 2027)}

def evaluate(sub, pm, seg, n_seeds):
    trades = [{"symbol": s, "signal_date": pd.Timestamp(d), "buy_date": bd, "exit_date": ed,
               "avg_pnl_pct": float(p), "entry": None, "priority": None,
               "pos_cap": None if pc is None else float(pc)}
              for s, d, bd, ed, p, pc in zip(sub["symbol"].values, sub["formed_at"].values,
                                             sub["entry_date"].values, sub["exit_date"].values,
                                             sub["avg_pnl_pct"].values,
                                             (sub["_cap"].values if "_cap" in sub else [None] * len(sub)))]
    p = replace(pm, quality_alloc=True) if "_cap" in sub else pm
    ms = [portfolio_metrics(trades, seg, cal,
                            position_model=replace(p, queue_order="random", queue_seed=s))
          for s in range(n_seeds)]
    return {"ann": float(np.median([m["ann"] for m in ms])) * 100,
            "dd": float(np.median([m["max_dd"] for m in ms])) * 100,
            "calmar": float(np.median([m["calmar"] for m in ms]))}

def run_arm(mask, cap=None):
    sub = BASE[mask.reindex(BASE.index).fillna(False)].copy()
    if cap is not None:
        sub["_cap"] = cap.reindex(sub.index).fillna(BASE_CAP)
    out = {}
    for k, pm in PMS.items():
        out[k] = evaluate(sub, pm, full, 21)
    out["years_B"] = [evaluate(sub, PMS["B"], seg, 11)["ann"] for seg in years.values()]
    out["kept_avg"] = float(sub["avg_pnl_pct"].mean())
    out["n"] = len(sub)
    return out

# 候选定义(pre=预注册)
BASE_CAP = 0.05
q_prem = BASE["premium"].quantile([.25, .5, .75])
hq = pd.qcut(BASE["heat_g"], 5, labels=False)
CANDS = [
    ("T1全空头带停新仓", BASE["cyb_ratio"] >= 1.0, None, True),
    ("缠线带节流", ~((BASE["cyb_ratio"] < 1.0) & (BASE["cyb_ratio"] >= 0.97)), None, True),
    ("T2分级(带停深半仓)", BASE["cyb_ratio"] >= 0.97,
     BASE["cyb_ratio"].map(lambda r: 0.05 if r >= 1.0 else 0.025), True),
    ("弃过热Q5(全期分位)", hq != 4, None, False),
    ("弃当日最热(日内截面)", BASE["heat_day"] <= 0.8, None, False),
    ("弃最弱板块Q1", ~(BASE["sec_r20"].fillna(0.5) < 0.25), None, False),
    ("弃死叉态(MACD)", ~(BASE["cross_age"].fillna(1) == 0), None, False),
    ("颈线带下切割", BASE["neck_pos"].fillna(0.5) >= 0, None, False),
    ("MA200斜率Q1闸", BASE["slp200"].fillna(0) >= 0.00536, None, False),
    ("溢价skip@3.0", ~(BASE["premium"] > 3.0), None, True),
    ("溢价skip@2.5", ~(BASE["premium"] > 2.5), None, True),
    ("溢价half@2.5", pd.Series(True, index=BASE.index),
     BASE["premium"].map(lambda x: 0.05 if x <= 2.5 else 0.025), True),
    ("量能门槛1.3", BASE["volr20"].fillna(1.0) >= 1.3, None, True),
    ("量能门槛1.5", BASE["volr20"].fillna(1.0) >= 1.5, None, True),
    ("momentum_gate0.15", BASE["mom20"].fillna(0) >= 0.15, None, True),
    ("min_rr2.5", BASE["rr"].fillna(0) >= 2.5, None, True),
    ("周五过滤(no_fri)", pd.to_datetime(BASE["formed_at"]).dt.weekday != 4, None, False),
    ("热度弹性仓位", pd.Series(True, index=BASE.index),
     hq.map({0: 0.065, 1: 0.06, 2: 0.05, 3: 0.05, 4: 0.03}), False),
    ("动量sizing(低减高加)", pd.Series(True, index=BASE.index),
     pd.cut(BASE["mom20"], [-1, 0, 0.10, 0.30, 10], labels=False).map(
         {0: 0.03, 1: 0.04, 2: 0.06, 3: 0.05}), False),
]

base_res = run_arm(pd.Series(True, index=BASE.index))
BASE_YEARS = base_res["years_B"]
print(f"\n基线: PM-B ann{base_res['B']['ann']:+.1f}% dd{base_res['B']['dd']:+.1f}% calmar{base_res['B']['calmar']:.2f} "
      f"| PM-A ann{base_res['A']['ann']:+.1f}% dd{base_res['A']['dd']:+.1f}%\n")
print(f"{'干预':<18}{'n':>6}{'B_ann':>7}{'B_dd':>7}{'B_cal':>6}{'A_ann':>7}{'V1':>4}{'V2':>4}{'V3':>4}{'V4':>4}{'V5':>4}  裁决")
results = {}
for name, mask, cap, pre in CANDS:
    r = run_arm(mask, cap)
    v1 = (r["kept_avg"] >= base_res["kept_avg"] - 0.02) and \
         (sum(1 for a, b in zip(r["years_B"], BASE_YEARS) if a >= b - 1e-9) >= 4)
    dd_better = r["B"]["dd"] - base_res["B"]["dd"] >= 1.0
    v2 = (r["B"]["ann"] >= base_res["B"]["ann"] - 1.0) and (dd_better or r["B"]["calmar"] >= base_res["B"]["calmar"])
    v3 = (r["A"]["ann"] >= base_res["A"]["ann"] - 1.0) or (base_res["A"]["dd"] - r["A"]["dd"] >= 2.0)
    v4 = (r["B"]["ann"] - base_res["B"]["ann"] >= 0.5) or \
         (base_res["B"]["dd"] - r["B"]["dd"] >= 3.0 and r["B"]["ann"] >= base_res["B"]["ann"] - 1.0)
    v5 = pre
    votes = sum((v1, v2, v3, v4, v5))
    verdict = "PASS" if (v2 and votes >= 4) else ("COND" if v2 and votes == 3 else "FAIL")
    breadth = r["n"] / base_res["n"]
    if verdict == "PASS" and breadth < 0.5:
        verdict = "COND(宽度塌缩)"
    results[name] = (r, verdict)
    print(f"{name:<18}{r['n']:>6}{r['B']['ann']:>+6.1f}%{r['B']['dd']:>+6.1f}%{r['B']['calmar']:>6.2f}"
          f"{r['A']['ann']:>+6.1f}%{'✓' if v1 else '✗':>4}{'✓' if v2 else '✗':>4}{'✓' if v3 else '✗':>4}"
          f"{'✓' if v4 else '✗':>4}{'✓' if v5 else '✗':>4}  {verdict}", flush=True)
# ── 叠加臂(PASS 幸存者组合) ──
print(chr(10) + "== 叠加臂 ==")
hq2 = pd.qcut(BASE["heat_g"], 5, labels=False)
stacks = [
    ("skip3.0+弃过热Q5", ~(BASE["premium"] > 3.0) & (hq2 != 4)),
    ("skip3.0+弃Q5+T1", ~(BASE["premium"] > 3.0) & (hq2 != 4) & (BASE["cyb_ratio"] >= 1.0)),
]
for name, mask in stacks:
    r = run_arm(mask)
    print(f"{name:<20} n={r['n']:>5} B_ann{r['B']['ann']:>+6.1f}% B_dd{r['B']['dd']:>+6.1f}% "
          f"calmar{r['B']['calmar']:.2f} A_ann{r['A']['ann']:>+6.1f}% | 分年 "
          + " ".join(f"{a:+5.1f}" for a in r["years_B"]), flush=True)

import json
Path("diag/retrial_verdicts.json").write_text(json.dumps(
    {k: {"B": v[0]["B"], "A": v[0]["A"], "verdict": v[1]} for k, v in results.items()},
    ensure_ascii=False, indent=1), encoding="utf-8")
print("\nsaved diag/retrial_verdicts.json")
