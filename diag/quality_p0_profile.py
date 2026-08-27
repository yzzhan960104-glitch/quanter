# -*- coding: utf-8 -*-
"""信号质量 P0 · 基线画像报告生成（读特征库 parquet → logs/quality/baseline_profile.md）。

纯后处理：不重扫，可反复重生成。产出（方案 §Phase 0「基线画像」）：
  1. 数据概况（笔数/年份/出场结构/收益分布）
  2. 五族特征分布（describe + NaN 率）
  3. 质量五维初步交叉表：
     - Q1 期望：逐特征五分位 × {n/均值/胜率}
     - Q2 稳定性预览：五分位 × 年段均值符号表（只描述不下结论——硬闸在 P1 预登记）
     - Q3 诚实池预览：wf 折 OOS 与主池同分桶方向对照（正式重证在 P1）
     - Q4 摩擦：组合层维度，P1/P2 处理（此处只登记）
     - Q5 拥挤度：同日信号数分桶 × 收益
  4. at_signal 特征相关性（|ρ|>0.6 的冗余对——P1 交互普查的先验）
  5. 红线重申（ADR-16 环境族只标注 / P2 特征域=at_signal）

用法：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_p0_profile.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

IN_PARQUET = "logs/quality/trades_features.parquet"
IN_META = "logs/quality/features_meta.json"
OUT_MD = "logs/quality/baseline_profile.md"

FAMILIES = {
    "几何族": ["h_atr", "rr_id", "touches", "n_tops", "bottom_disp",
               "pattern_days", "neckline_span", "suppression", "H", "atr"],
    "动量族": ["ret5", "ret20", "ret60", "pos_high60", "breakout_ret", "atr_pct"],
    "量能族": ["bvr", "vol5_slope", "pv_corr10"],
    "位置族(at_fill)": ["rr_exec", "entry_depth_atr", "wait_days", "is_chase"],
    "环境族(ADR-16 只标注)": ["pool_mom20", "pool_vol20"],
}
ALL_FEATS = [c for cols in FAMILIES.values() for c in cols]
AT_SIGNAL = ["h_atr", "rr_id", "touches", "n_tops", "bottom_disp",
             "pattern_days", "neckline_span", "suppression", "ret5", "ret20",
             "ret60", "pos_high60", "breakout_ret", "atr_pct", "bvr",
             "vol5_slope", "pv_corr10", "same_day_n"]
YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


def _qcut5(s: pd.Series) -> pd.Series:
    """五分位桶（重复值边界兜底 rank；NaN 保留 NaN）。"""
    try:
        return pd.qcut(s, 5, duplicates="drop")
    except ValueError:
        return pd.qcut(s.rank(method="first"), 5, duplicates="drop")


def main():
    df = pd.read_parquet(IN_PARQUET)
    meta = json.load(open(IN_META, encoding="utf-8"))
    main_df = df[df["pool"] == "main"].copy()
    L = []
    ap = L.append

    ap("# 信号质量基线画像（P0 · 2026-08-26）")
    ap("")
    ap("> 特征库 `logs/quality/trades_features.parquet`；口径锚 = outer 2026 冻结口径 "
       f"ann {meta['anchor_outer2026_freeze']['ann']:+.1%} / taken "
       f"{meta['anchor_outer2026_freeze']['n_taken']}（复现 r6_11c on）。")
    ap("> B3 ACTIVE 参数（neckline_r610_b3_20260826）× freeze(2021-01-01) 今日截面池；"
       "exec 约定=只含成交笔。")
    ap("> **本报告只做描述性画像，不产出存活结论**——判别力硬闸（≥4/5 年同向 + wf 诚实池"
       "重证 + 组合口径增益）在 P1 预登记后执行。")
    ap("")
    ap("红线重申：①环境族（pool_mom20/pool_vol20）ADR-16 只标注不过滤、不进任何决策路径；"
       "②P2 分层分合法特征域=at_signal（signal_date 收盘可知）；rr_exec/entry_depth/"
       "wait_days/is_chase 为成交后才可知，仅归因用；③C2 内核冻结未触碰。")
    ap("")

    # ── 1. 数据概况 ──
    ap("## 1. 数据概况")
    ap("")
    ap(f"- 总笔数 {len(df)}（主池今日截面 {len(main_df)} + wf 诚实池 "
       + " + ".join(f"{p} {n}" for p, n in
                   [(p, n) for p, n in meta["pools"].items() if p != "main"]) + "）")
    ap("")
    ap("### 主池按年段")
    yr = main_df.groupby("year").agg(
        n=("avg_pnl_pct", "size"), mean=("avg_pnl_pct", "mean"),
        median=("avg_pnl_pct", "median"), win=("win", "mean")).round(3)
    ap(yr.to_string())
    ap("")
    ap("### 出场结构（主池全期）")
    ex = main_df.groupby("exit_reason").agg(
        n=("avg_pnl_pct", "size"), mean=("avg_pnl_pct", "mean"),
        median=("avg_pnl_pct", "median"), win=("win", "mean"),
        hold=("holding_bars", "mean")).round(3).sort_values("n", ascending=False)
    ap(ex.to_string())
    ap("")

    # ── 2. 特征分布 ──
    ap("## 2. 五族特征分布（主池）")
    ap("")
    for fam, cols in FAMILIES.items():
        ap(f"### {fam}")
        desc = main_df[cols].describe(percentiles=[0.25, 0.5, 0.75]).T
        desc["nan%"] = (main_df[cols].isna().mean() * 100).round(1)
        ap(desc[["count", "nan%", "mean", "25%", "50%", "75%", "max"]].round(3).to_string())
        ap("")

    # ── 3. Q1 期望交叉表：特征五分位 × 收益 ──
    ap("## 3. Q1 期望 · 逐特征五分位交叉表（主池，avg_pnl_pct）")
    ap("")
    ap("五分位 Q1(低)→Q5(高)；「ΔQ5−Q1」为两端均值差（pp）。单调性仅描述。")
    ap("")
    q1_rows = []
    for c in ALL_FEATS:
        s = main_df[c].dropna()
        if len(s) < 500:
            continue
        b = _qcut5(main_df[c])
        g = main_df.groupby(b, observed=True)["avg_pnl_pct"]
        means = g.mean()
        wins = main_df.groupby(b, observed=True)["win"].mean()
        if len(means) < 3:
            continue
        q1_rows.append({
            "feature": c, "n": int(len(s)),
            "Q1": round(means.iloc[0], 2), "Q2": round(means.iloc[1], 2),
            "Q3": round(means.iloc[2], 2),
            "Q4": round(means.iloc[3], 2) if len(means) > 4 else None,
            "Q5": round(means.iloc[-1], 2),
            "ΔQ5−Q1": round(means.iloc[-1] - means.iloc[0], 2),
            "胜率Q1": round(wins.iloc[0], 3), "胜率Q5": round(wins.iloc[-1], 3),
        })
    ap(pd.DataFrame(q1_rows).to_string(index=False))
    ap("")

    # ── 4. Q2 稳定性预览：特征两端组 × 年段均值 ──
    ap("## 4. Q2 稳定性预览 · 特征两端组（bottom30% vs top30%）× 年段均值")
    ap("")
    ap("描述性预览：**同一端在各年段的符号是否一致**（正式 ≥4/5 年同向硬闸在 P1 对全分布"
       "分桶执行并预登记）。")
    ap("")
    rows = []
    for c in [x for x in ALL_FEATS if x not in ("H", "atr", "n_tops")]:
        s = main_df[c].dropna()
        if len(s) < 500:
            continue
        lo_t, hi_t = s.quantile([0.3, 0.7])
        lo = main_df[main_df[c] <= lo_t]
        hi = main_df[main_df[c] >= hi_t]
        row = {"feature": c, "n_lo": len(lo), "n_hi": len(hi)}
        for y in YEARS:
            a = lo[lo["year"] == y]["avg_pnl_pct"]
            b = hi[hi["year"] == y]["avg_pnl_pct"]
            row[f"lo{y}"] = round(a.mean(), 2) if len(a) >= 20 else None
            row[f"hi{y}"] = round(b.mean(), 2) if len(b) >= 20 else None
        rows.append(row)
    ap(pd.DataFrame(rows).to_string(index=False))
    ap("")

    # ── 5. Q5 拥挤度 ──
    ap("## 5. Q5 拥挤度 · 同日信号数 × 收益（主池）")
    ap("")
    bins = [0, 2, 5, 10, 20, 40, 80, 10 ** 9]
    lab = ["≤2", "3-5", "6-10", "11-20", "21-40", "41-80", ">80"]
    gb = main_df.groupby(pd.cut(main_df["same_day_n"], bins, labels=lab),
                         observed=True).agg(
        n=("avg_pnl_pct", "size"), mean=("avg_pnl_pct", "mean"),
        win=("win", "mean")).round(3)
    ap(gb.to_string())
    ap("")

    # ── 6. Q3 诚实池预览 ──
    ap("## 6. Q3 诚实池预览 · wf 折 OOS vs 主池同特征两端组方向对照")
    ap("")
    ap("非正式对照（正式重证=P1 在 wf 2022/2024 折上对存活特征全分桶验证）。"
       "两端组口径同 §4（bottom30%/top30%，均值 pp）。")
    ap("")
    folds = [p for p in df["pool"].unique() if p != "main"]
    rows = []
    for c in ["h_atr", "rr_id", "touches", "suppression", "ret20", "ret60",
              "pos_high60", "atr_pct", "bvr", "pv_corr10", "pattern_days",
              "bottom_disp", "vol5_slope"]:
        sm = main_df[c].dropna()
        if len(sm) < 500:
            continue
        lo_t, hi_t = sm.quantile([0.3, 0.7])
        row = {"feature": c,
               "主池lo/hi": (f"{main_df[main_df[c] <= lo_t]['avg_pnl_pct'].mean():+.2f}"
                             f"/{main_df[main_df[c] >= hi_t]['avg_pnl_pct'].mean():+.2f}")}
        for f in folds:
            ff = df[(df["pool"] == f) & (~df["embargoed"])]
            sf = ff[c].dropna()
            if len(sf) < 200:
                row[f] = "n<200"
                continue
            flo = ff[ff[c] <= lo_t]["avg_pnl_pct"]
            fhi = ff[ff[c] >= hi_t]["avg_pnl_pct"]
            row[f] = (f"{flo.mean():+.2f}/{fhi.mean():+.2f}"
                      if len(flo) >= 20 and len(fhi) >= 20 else "稀")
        rows.append(row)
    ap(pd.DataFrame(rows).to_string(index=False))
    ap("")
    ap("（格式 lo/hi = 底 30% 组均值 / 顶 30% 组均值，pp；方向与主池一致=分层方向在诚实池"
       "可复现的初步信号）")
    ap("")

    # ── 7. at_signal 特征冗余 ──
    ap("## 7. at_signal 特征相关性（|ρ|>0.6 对，P1 交互普查先验）")
    ap("")
    cm = main_df[AT_SIGNAL].corr(method="spearman")
    seen = set()
    pairs = []
    for i in cm.index:
        for j in cm.columns:
            if i >= j:
                continue
            v = cm.loc[i, j]
            if abs(v) > 0.6:
                pairs.append((i, j, round(v, 3)))
    ap("冗余对 " + str(len(pairs)) + " 个：" if pairs else "无 |ρ|>0.6 的冗余对。")
    if pairs:
        ap("")
        for i, j, v in sorted(pairs, key=lambda x: -abs(x[2])):
            ap(f"- {i} × {j}: ρ={v}")
    ap("")

    ap("## 8. Q4 摩擦存活性（登记）")
    ap("")
    ap("摩擦（整手+min5+冻结）对特征桶的边际差异是组合层维度，P1 的「组合口径增益」闸与 "
       "P2 的受控 A/B 在冻结口径下执行——本画像不预判。")
    ap("")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[done] {OUT_MD} ({len(L)} 行)")


if __name__ == "__main__":
    main()
