# -*- coding: utf-8 -*-
"""信号质量 P1 · 判别力普查（2026-08-26 方案 Phase 1）。

对 P0 特征库逐特征 + top 交互做判别力普查，预登记四闸（先于看数写死在此）：

  G1 判别力（量级+显著性，Bonferroni 收紧）：
     |Spearman IC| 的 p < 0.05/n_tests（n_tests=单特征数+交互数；任务红线 >50 才强制
     Bonferroni，此处无条件执行=更保守）且 |Q5−Q1| ≥ 0.5pp；
  G2 年段一致性硬闸：方向 d=sign(总体 IC)；2022-2026 五年中 hi30−lo30 对比与 d 同号
     的年数 ≥4（样本不足年计为不同向——保守；单年强不收）；
  G3 wf 诚实池重证：wf1(oos 2022) 与 wf2(oos 2024) 两折（折末选股池、去 embargo）的
     hi30−lo30 对比方向均与 d 一致（任一折样本 <20/侧 即不过）；
  G4 ADR-16：环境族（pool_mom20/pool_vol20）不入候选集（只标注）。

存活后附加分类（判别力≠可治疗性预案，非闸）：组合口径过滤测试——冻结核下
bottom-30% 剔除 vs 全量的 outer 2026 ann 对比；无增益 → 记「分层候选」（P2 语义）。

交互：G1 通过的连续特征中 |IC| top6 两两 z-score 乘积（≤15 对），同一套 G1-G3。

产物：logs/quality/discriminance_report.md + logs/quality/survivors.json

用法：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_p1_discriminance.py
"""
import json
import os
import sys
from itertools import combinations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from scipy import stats

IN_PARQUET = "logs/quality/trades_features.parquet"
OUT_DIR = "logs/quality"

# —— 预登记候选集（at_signal 优先 + at_fill 仅归因；环境族排除）——
SINGLES_AT_SIGNAL = ["h_atr", "rr_id", "touches", "n_tops", "bottom_disp",
                     "pattern_days", "neckline_span", "suppression", "ret5",
                     "ret20", "ret60", "pos_high60", "breakout_ret", "atr_pct",
                     "bvr", "vol5_slope", "pv_corr10", "same_day_n"]
SINGLES_AT_FILL = ["rr_exec", "entry_depth_atr", "wait_days", "is_chase"]
YEARS = [2022, 2023, 2024, 2025, 2026]
FOLD_GATES = {"wf1_2020_21": "oos 2022", "wf2_2022_23": "oos 2024"}
MIN_DQ51 = 0.5          # |Q5−Q1| 量级闸（pp）
TOP_K_INTERACT = 6


def _contrast(df, col, lo_q=0.3, hi_q=0.7, min_n=20):
    """hi30−lo30 组均值差（pp）。样本不足返 None。阈值从该 df 自身分位计算。"""
    s = df[col].dropna()
    if len(s) < 200:
        return None
    lo_t, hi_t = s.quantile([lo_q, hi_q])
    lo = df.loc[df[col] <= lo_t, "avg_pnl_pct"]
    hi = df.loc[df[col] >= hi_t, "avg_pnl_pct"]
    if len(lo) < min_n or len(hi) < min_n:
        return None
    return float(hi.mean() - lo.mean())


def _quintile_table(df, col):
    s = df[col].dropna()
    if len(s) < 500:
        return None
    try:
        b = pd.qcut(df[col], 5, duplicates="drop")
    except ValueError:
        b = pd.qcut(df[col].rank(method="first"), 5, duplicates="drop")
    g = df.groupby(b, observed=True)["avg_pnl_pct"].mean()
    return [round(v, 2) for v in g.tolist()], float(g.iloc[-1] - g.iloc[0])


def evaluate_feature(df_main, folds_df, col, alpha, n_tests):
    """单特征/交互列的完整四闸评估。返回 dict（含所有中间量，报告直出）。"""
    s = df_main[col].dropna()
    res = {"feature": col, "n": int(len(s))}
    if len(s) < 500:
        res["gate_G1"] = False
        res["why"] = "n<500"
        return res
    ic, p = stats.spearmanr(s, df_main.loc[s.index, "avg_pnl_pct"])
    qtab = _quintile_table(df_main, col)
    res["ic"] = round(float(ic), 4)
    res["ic_p"] = float(p)
    res["q_means"], res["dq51"] = qtab
    res["bonf_p"] = alpha / n_tests
    g1 = (p < alpha / n_tests) and (abs(res["dq51"]) >= MIN_DQ51)
    res["gate_G1"] = bool(g1)
    d = 1 if ic >= 0 else -1
    res["direction"] = d

    # G2：年段一致性（样本不足年=不同向，保守）
    same, yr_detail = 0, {}
    for y in YEARS:
        dy = df_main[df_main["year"] == y]
        c = _contrast(dy, col)
        yr_detail[y] = None if c is None else round(c, 2)
        if c is not None and np.sign(c) == d:
            same += 1
    res["year_contrasts"] = yr_detail
    res["years_same"] = same
    res["gate_G2"] = same >= 4

    # G3：wf 折重证（两折均须同向）
    fold_detail = {}
    g3 = True
    for f, label in FOLD_GATES.items():
        ff = folds_df.get(f)
        if ff is None or len(ff) == 0:
            fold_detail[f] = None
            g3 = False
            continue
        c = _contrast(ff, col)
        fold_detail[f] = None if c is None else round(c, 2)
        if c is None or np.sign(c) != d:
            g3 = False
    res["fold_contrasts"] = fold_detail
    res["gate_G3"] = bool(g3)
    res["survivor"] = bool(g1 and res["gate_G2"] and g3)
    return res


def _zcols(frame, cols):
    """池内 z-score 标准化交互列（每池独立标准化——诚实池各自成分布）。"""
    out = {}
    for c in cols:
        s = frame[c].astype(float)
        mu, sd = s.mean(), s.std()
        if sd and sd > 0:
            out[c] = (s - mu) / sd
    return out


def _portfolio_ann(filled, seg, udates, pm):
    from discovery.objective import portfolio_metrics
    m = portfolio_metrics(filled, seg, udates, position_model=pm)
    return {"ann": round(m["ann"], 4), "n_taken": m["n_taken"],
            "win": round(m["win_rate"], 3), "max_dd": round(m["max_dd"], 4)}


def main():
    from datetime import date
    from backtest.models import PositionModel
    from discovery.split import Segment, holdout_split

    df = pd.read_parquet(IN_PARQUET)
    main_df = df[df["pool"] == "main"].copy()
    folds_df = {}
    for f in FOLD_GATES:
        ff = df[(df["pool"] == f) & (~df["embargoed"])].copy()
        folds_df[f] = ff
        print(f"[fold] {f}: {len(ff)} 笔", flush=True)
    print(f"[main] {len(main_df)} 笔", flush=True)

    alpha = 0.05
    singles = SINGLES_AT_SIGNAL + SINGLES_AT_FILL

    # —— 阶段 A：单特征 G1 预筛（定交互候选 + n_tests）——
    # n_tests 预算 = 单特征数 + 交互上界（top6 两两 15 对），显著性阈值一次定死
    n_tests = len(singles) + TOP_K_INTERACT * (TOP_K_INTERACT - 1) // 2
    bonf = alpha / n_tests
    print(f"[prereg] n_tests={n_tests} bonf_alpha={bonf:.2e} "
          f"|dq51|>={MIN_DQ51}pp 年段闸≥4/5 wf双折同向", flush=True)

    pre = []
    for c in singles:
        s = main_df[c].dropna()
        if len(s) < 500:
            continue
        ic, p = stats.spearmanr(s, main_df.loc[s.index, "avg_pnl_pct"])
        q = _quintile_table(main_df, c)
        pre.append({"col": c, "ic": ic, "p": p, "dq51": q[1]})
    g1_pass = [r for r in pre if r["p"] < bonf and abs(r["dq51"]) >= MIN_DQ51]
    # 完美共线去重（预登记）：交互候选按 |IC| 降序贪心保留，与已选者 |ρ|>0.95
    # 的跳过（rr_id≡h_atr 类参数退化的重复表征不占交互槽位；单特征四闸照测不改）
    _cands_sorted = sorted(g1_pass, key=lambda x: -abs(x["ic"]))
    kept_c, dropped = [], []
    for r in _cands_sorted:
        if r["col"] not in SINGLES_AT_SIGNAL:
            continue
        dup = False
        for k in kept_c:
            rho = main_df[r["col"]].corr(main_df[k["col"]], method="spearman")
            if rho is not None and abs(rho) > 0.95:
                dropped.append((r["col"], k["col"], round(rho, 3)))
                dup = True
                break
        if not dup:
            kept_c.append(r)
        if len(kept_c) >= TOP_K_INTERACT:
            break
    interact_cands = [r["col"] for r in kept_c]
    print(f"[stageA] G1 过 {len(g1_pass)}/{len(pre)}；共线剔除 {dropped}；"
          f"交互候选 {interact_cands}", flush=True)

    # —— 交互列构造（各池独立 z 标准化）——
    inter_pairs = list(combinations(interact_cands, 2))
    for frame in [main_df] + list(folds_df.values()):
        z = _zcols(frame, interact_cands)
        for a, b in inter_pairs:
            if a in z and b in z:
                frame[f"{a}*{b}"] = z[a] * z[b]
    inter_cols = [f"{a}*{b}" for a, b in inter_pairs]

    # —— 阶段 B：全量四闸评估（单特征 + 交互）——
    all_cols = [c for c in singles if c in main_df.columns] + inter_cols
    results = [evaluate_feature(main_df, folds_df, c, alpha, n_tests)
               for c in all_cols]
    survivors = [r for r in results if r.get("survivor")]
    print(f"[stageB] 存活 {len(survivors)}：{[r['feature'] for r in survivors]}",
          flush=True)

    # —— 存活后分类：组合口径过滤测试（判别力≠可治疗性预案）——
    split = holdout_split()
    full_seg = Segment("full", date(2021, 1, 1), date(2026, 12, 31))
    # 市场日历（ann 分母）：湖 date 列全量——信号日并集是稀疏日历，n_days 偏小
    # 会虚高 ann（首版实测 4.126 vs 标准 4.020 即此坑）。date 是索引元数据，
    # 用 pyarrow 直读列（pandas columns= 拿不到索引列）。
    import pyarrow.parquet as pq
    _dates = pq.read_table("data_lake/a_shares_daily.parquet",
                           columns=["date"]).column("date").to_pandas()
    udates = pd.DatetimeIndex(sorted(_dates.unique()))
    udates = udates[udates >= pd.Timestamp("2021-01-01")]
    pm_freeze = PositionModel(capital=1_000_000, lot_size=100, min_fee=5.0,
                              max_positions=4, pos_cap=0.075,
                              freeze_pending=True)
    to_filled = lambda d: [{"symbol": r["symbol"], "signal_date": r["signal_date"],
                            "buy_date": r["buy_date"], "exit_date": r["exit_date"],
                            "avg_pnl_pct": r["avg_pnl_pct"], "entry": r["entry"]}
                           for _, r in d.iterrows()]
    base_outer = to_filled(main_df[main_df["signal_date"].apply(split.outer.covers)])
    base_full = to_filled(main_df)
    ann_base_outer = _portfolio_ann(base_outer, split.outer, udates, pm_freeze)
    ann_base_full = _portfolio_ann(base_full, full_seg, udates, pm_freeze)
    print(f"[baseline] outer={ann_base_outer} full={ann_base_full}", flush=True)
    for sv in survivors:
        c = sv["feature"]
        d = sv["direction"]
        lo_t, hi_t = main_df[c].dropna().quantile([0.3, 0.7])
        worst_side = (main_df[c] <= lo_t) if d > 0 else (main_df[c] >= hi_t)
        kept = main_df[~worst_side]
        sig_uplift = kept["avg_pnl_pct"].mean() - main_df["avg_pnl_pct"].mean()
        fo = to_filled(kept[kept["signal_date"].apply(split.outer.covers)])
        ff_ = to_filled(kept)
        sv["filter_test"] = {
            "drop": "bottom30" if d > 0 else "top30",
            "n_dropped_outer": len(base_outer) - len(fo),
            "signal_mean_uplift_pp": round(float(sig_uplift), 3),
            "outer": _portfolio_ann(fo, split.outer, udates, pm_freeze),
            "full": _portfolio_ann(ff_, full_seg, udates, pm_freeze),
        }
        gain = sv["filter_test"]["outer"]["ann"] - ann_base_outer["ann"]
        sv["classification"] = ("filter_candidate" if gain > 0 else
                                "layering_candidate")
        print(f"[class] {c}: 外层 ann Δ{gain:+.3f} → {sv['classification']}",
              flush=True)
    # —— 环境族只做标注表（不入候选，ADR-16）——
    env_label = {}
    for c in ["pool_mom20", "pool_vol20"]:
        ic, p = stats.spearmanr(main_df[c].dropna(),
                                main_df.loc[main_df[c].dropna().index, "avg_pnl_pct"])
        env_label[c] = {"ic": round(float(ic), 4), "p": float(p),
                        "note": "ADR-16 只标注不过滤，不入存活清单"}

    # —— 产物 ——
    os.makedirs(OUT_DIR, exist_ok=True)
    report = {
        "preregistration": {
            "gates": ["G1 |IC| Bonferroni p<{:.2e} & |Q5-Q1|>=0.5pp".format(bonf),
                      "G2 >=4/5 年(2022-26) hi30-lo30 同向（缺样年计异向）",
                      "G3 wf oos2022 & oos2024 双折同向",
                      "G4 环境族 ADR-16 排除"],
            "n_tests": n_tests, "candidates": singles,
            "interaction_pairs": [list(p) for p in inter_pairs],
            "min_bucket_n": 20,
        },
        "baseline_ann": {"outer2026_freeze": ann_base_outer,
                         "full2021_26_freeze": ann_base_full},
        "survivors": survivors,
        "env_label_only": env_label,
        "all_features": results,
    }
    with open(os.path.join(OUT_DIR, "survivors.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=str)
    print(f"[done] {OUT_DIR}/survivors.json", flush=True)

    # —— discriminance_report.md（人读版，与 json 同源）——
    L = []
    ap = L.append
    ap("# 判别力普查报告（P1 · 2026-08-26）")
    ap("")
    ap("> 特征库：`logs/quality/trades_features.parquet`（主池今日截面 + wf 诚实池）。")
    ap("> 预登记四闸（先于看数写死于脚本头部）：G1 判别量级+Bonferroni 显著性"
       f"（p<{bonf:.2e}，n_tests={n_tests}）∧ |Q5−Q1|≥{MIN_DQ51}pp；"
       "G2 年段硬闸 ≥4/5（2022-26）同向；G3 wf oos2022+oos2024 双折同向；"
       "G4 环境族 ADR-16 排除。")
    ap(f"> 基线复核：outer 2026 冻结口径 ann {ann_base_outer['ann']:+.1%} / "
       f"taken {ann_base_outer['n_taken']}；全期(2021-26) ann "
       f"{ann_base_full['ann']:+.1%} / taken {ann_base_full['n_taken']}。")
    ap("")
    ap(f"**存活 {len(survivors)} 个**：" +
       ("、".join(f"`{s['feature']}`" for s in survivors) if survivors else "（无）"))
    ap("")
    ap("## 存活特征明细")
    ap("")
    for s in survivors:
        ap(f"### {s['feature']}（方向 {'高好' if s['direction'] > 0 else '低好'}，"
           f"IC={s['ic']:+.4f}，ΔQ5−Q1={s['dq51']:+.2f}pp）")
        ap("")
        ap(f"- 五分位均值：{s['q_means']}")
        ap(f"- 年段对比 hi30−lo30（2022-26）：{s['year_contrasts']} "
           f"→ 同向 {s['years_same']}/5")
        ap(f"- wf 折对比：{s['fold_contrasts']}")
        ft = s.get("filter_test", {})
        ap(f"- 组合口径过滤测试（{ft.get('drop')}，剔 "
           f"{ft.get('n_dropped_outer')} 笔/外层）：信号均值 "
           f"{ft.get('signal_mean_uplift_pp', 0):+.2f}pp；outer ann "
           f"{ft['outer']['ann']:+.1%}（Δ{ft['outer']['ann'] - ann_base_outer['ann']:+.3f}）"
           f"；全期 ann {ft['full']['ann']:+.1%}")
        ap(f"- **分类：{s['classification']}**"
           + ("（组合无增益→分层候选，不过滤——三案预案）"
              if s["classification"] == "layering_candidate" else "（组合有增益）"))
        ap("")
    ap("## 全特征四闸明细（含未存活）")
    ap("")
    rows = []
    for r in results:
        rows.append({
            "feature": r["feature"], "n": r.get("n"),
            "IC": r.get("ic"), "ΔQ5−Q1": r.get("dq51"),
            "G1": r.get("gate_G1", False),
            "年同向": f"{r.get('years_same', 0)}/5" if r.get("year_contrasts") else "",
            "G3双折": ("过" if r.get("gate_G3") else
                       str(r.get("fold_contrasts"))),
            "存活": r.get("survivor", False),
        })
    ap(pd.DataFrame(rows).to_string(index=False))
    ap("")
    ap("## 环境族标注（ADR-16 · 只标注不过滤）")
    ap("")
    for c, v in env_label.items():
        ap(f"- {c}: IC={v['ic']:+.4f}（p={v['p']:.2e}）——不进存活清单、不进任何决策路径")
    ap("")
    with open(os.path.join(OUT_DIR, "discriminance_report.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[done] {OUT_DIR}/discriminance_report.md", flush=True)


if __name__ == "__main__":
    main()
