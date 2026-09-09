# -*- coding: utf-8 -*-
"""因子动物园批量扫描(2026-09-05 24h 审计 · 步骤B)。

对 diag/factor_zoo.parquet 全因子统一口径:
  ① 分年 Spearman rank-IC(因子 vs avg_pnl_pct),六年均值/标准差/t 统计/同号年数
  ② 双窗(inner 2025 / outer 2026)四分位 Q4−Q1 价差(inner 定桶套 outer)
  ③ 多重检验:BH-FDR(q=0.10)在 ~20 因子上校正;同时报告「名义 5% 通过数 vs 期望假阳性数」
  ④ 幸存判据(三关):|t|≥2.5 且 同号年≥5/6 且 双窗同号 → 进组合口径
weekday 为类别变量单列处理。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd
from scipy import stats

F = pd.read_parquet("diag/factor_zoo.parquet")
FACTORS = ["turnover", "turnover20", "log_mv", "pe_rank", "pb_rank",
           "elg_ratio", "mf_ratio", "elg5",
           "winner_rate", "chip_conc", "pdev",
           "kdj_k", "rsi6", "cci",
           "rv_ratio", "beta1000", "idio", "gap20", "pos20",
           "north5", "inst_net10"]
YEARS = list(range(2021, 2027))
inner = F[F.seg == "inner"]
outer = F[F.seg == "outer"]
print(f"n={len(F)} inner={len(inner)} outer={len(outer)}  基线均笔 六年{F.avg_pnl_pct.mean():+.2f}% "
      f"inner{inner.avg_pnl_pct.mean():+.2f}% outer{outer.avg_pnl_pct.mean():+.2f}%\n")

rows = []
for f in FACTORS:
    d = F.dropna(subset=[f])
    if len(d) < 5000:
        rows.append({"factor": f, "n": len(d), "note": "样本不足"}); continue
    ics = []
    for y in YEARS:
        dy = d[d.year == y]
        if len(dy) < 300:
            continue
        ic, _ = stats.spearmanr(dy[f], dy["avg_pnl_pct"])
        ics.append(ic)
    ics = np.array(ics)
    mean_ic, sd_ic = ics.mean(), ics.std(ddof=1)
    t = mean_ic / (sd_ic / np.sqrt(len(ics))) if sd_ic > 0 else np.nan
    p = 2 * stats.t.sf(abs(t), df=len(ics) - 1) if np.isfinite(t) else np.nan
    same = (np.sign(ics) == np.sign(mean_ic)).sum()
    # 双窗四分位价差(inner 定桶)
    di = inner.dropna(subset=[f]); do = outer.dropna(subset=[f])
    qs = di[f].quantile([.25, .5, .75]).values
    zero_inflated = len(np.unique(qs)) < 3
    if zero_inflated:
        # 稀疏事件因子(如龙虎榜):二分 有事件(>0) vs 无,Q4−Q1 语义=有−无
        def spread(dd):
            b = (dd[f] > 0).astype(int) * 3
            m = dd.groupby(b)["avg_pnl_pct"].mean()
            return (m.get(3, np.nan) - m.get(0, np.nan)), m
    else:
        edges = np.r_[-np.inf, qs, np.inf]
        def spread(dd):
            b = pd.cut(dd[f], edges, labels=False, include_lowest=True, duplicates="drop")
            m = dd.groupby(b)["avg_pnl_pct"].mean()
            return (m.get(3, np.nan) - m.get(0, np.nan)), m
    sp_i, mi = spread(di); sp_o, mo = spread(do)
    rows.append({"factor": f, "n": len(d), "IC均": mean_ic, "IC_t": t, "p": p,
                 "同号年": f"{same}/{len(ics)}", "inner_Q4-Q1": sp_i, "outer_Q4-Q1": sp_o,
                 "双窗同号": np.sign(sp_i) == np.sign(sp_o),
                 "inner四分位": "/".join(f"{v:+.2f}" for v in mi.values),
                 "outer四分位": "/".join(f"{v:+.2f}" for v in mo.values)})

R = pd.DataFrame(rows)
ok = R["p"].notna()
# BH-FDR
pv = R.loc[ok, "p"].values
order = np.argsort(pv); m = len(pv)
bh = np.empty(m); bh[order] = np.minimum.accumulate((pv[order] * m / np.arange(1, m + 1))[::-1])[::-1]
R.loc[ok, "q_BH"] = np.clip(bh, 0, 1)
R["三关幸存"] = ok & (R["IC_t"].abs() >= 2.5) & R["同号年"].str.split("/").str[0].astype(float).ge(5) & R["双窗同号"].fillna(False)
R = R.sort_values("IC_t", key=lambda s: s.abs(), ascending=False)

pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)
print("== 因子排名(按 |IC_t|) ==")
print(R[["factor", "n", "IC均", "IC_t", "p", "q_BH", "同号年", "inner_Q4-Q1", "outer_Q4-Q1", "双窗同号", "三关幸存"]]
      .to_string(index=False, float_format=lambda x: f"{x:+.3f}" if abs(x) < 10 else f"{x:.0f}"))
print(f"\n名义 p<0.05 通过: {(R['p'] < .05).sum()} 个 / 期望假阳性≈{0.05*ok.sum():.1f} 个;"
      f"  BH q<0.10 通过: {(R['q_BH'] < .10).sum()} 个;  三关幸存: {R['三关幸存'].sum()} 个")
print("\n== 双窗四分位明细(Q1→Q4) ==")
for _, r in R.iterrows():
    if pd.notna(r.get("IC_t")):
        print(f"  {r['factor']:<12} inner {r['inner四分位']:<28} outer {r['outer四分位']}")

# weekday(类别)
print("\n== weekday(0=周一) ==")
for nm, d in (("inner", inner), ("outer", outer), ("六年", F)):
    g = d.groupby("weekday")["avg_pnl_pct"].agg(["size", "mean"])
    print(f"  {nm:<5} " + " | ".join(f"周{int(k)+1} n={int(v['size'])} {v['mean']:+.2f}%" for k, v in g.iterrows()))
R.to_csv("diag/factor_zoo_scan.csv", index=False, encoding="utf-8-sig")
print("\nsaved diag/factor_zoo_scan.csv")
