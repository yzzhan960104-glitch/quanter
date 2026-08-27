# -*- coding: utf-8 -*-
"""H-R7d · 吞吐感知可部署优先级（R7 台账登记的下假设，2026-08-27 发射）。

════════════════════════════════════════════════════════════════════
预登记协议（先于看数写死——此刻未看过任何槽位效率判别力/优先级 A/B 读数）
════════════════════════════════════════════════════════════════════
机制（R7c/R8 的结论链）：冻结核下可部署口径的稀缺资源=槽位天（4 槽），oracle
调度（exit_date 序）靠未来信息挑最快了结者吃到 445 taken vs 可部署 ~60 的吞吐
差；R8 证明参数面两口径均到顶。假设：用 at_signal 特征（signal_date 收盘可知）
预测候选的**槽位效率**并优先进场，可部署地赎回部分调度保费。

两阶段：
Stage 1 · 判别力普查（特征库现成，目标变量换轨）：
  目标 t_eff = avg_pnl_pct / max(occupancy_days,1)，occupancy_days =
  wait_days + holding_bars（交易日口径，成交后可知=合法目标变量，预测特征
  仍限 at_signal）；副目标 t_occ = occupancy_days（纯吞吐轴）。
  候选 = 18 个 at_signal 特征 + 主目标 G1 过筛者 top6 两两 z 交互（共线去重
  ρ>0.95，同 P1）；Bonferroni n_tests=51。
  闸（对主目标 t_eff，逐特征）：G1 |IC| p<9.8e-4 ∧ ΔQ5−Q1≥0.03pp/槽天
  （均值≈0.086 的 ~35%）；G2 年段 4/5 同向（hi30−lo30，缺样年计异向）；
  G3 wf oos2022+oos2024 双折同向；G4 环境族 ADR-16 排除。
Stage 2 · 受控 A/B（有主目标存活者才做）：
  主型分数 = t_eff 存活特征方向对齐 z 等权（训练段 2022-24 池内标准化）；
  副型 = t_occ 存活特征取负（预测占用短者优先）。
  臂：priority 序（queue_order=priority，确定性）vs random 21 种子。
  闸（全过=PASS；任一否=VETO）：
  ①A1 inner(21-24)：ann_pri > median(ann_random) 且胜 ≥15/21 种子；
  ②A2 outer(25-26) holdout：同款两条件（一票否决）；
  ③A3 inner 逐年（21-24）：对 random 中位无恶化 >10pp；
  ④A4 换手下限：inner n_taken(pri) ≥ 0.7 × median n_taken(random)；
  ⑤A5 红线：参数零改动 / C2 零触碰 / 分数仅用 at_signal / ADR-16。
认识论声明：本测为 R7 台账登记假设的首次受控检验；PASS 仍是回测级结论，
部署级确认须双轨实测或前向窗（R7a 同款约束）。
════════════════════════════════════════════════════════════════════

用法：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_r7d_throughput.py
"""
import json
import os
import sys
from itertools import combinations
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from scipy import stats

from diag.quality_p2_layering import _ann, _pm, _to_filled
from diag.quality_r7c_priority import vector_score

OUT_DIR = "logs/quality"
AT_SIGNAL = ["h_atr", "rr_id", "touches", "n_tops", "bottom_disp",
             "pattern_days", "neckline_span", "suppression", "ret5", "ret20",
             "ret60", "pos_high60", "breakout_ret", "atr_pct", "bvr",
             "vol5_slope", "pv_corr10", "same_day_n"]
YEARS = [2022, 2023, 2024, 2025, 2026]
MIN_EFF_DQ = 0.03          # pp/槽天，主目标量级闸
MIN_OCC_DQ = 2.0           # 天，副目标量级闸
N_TESTS = 18 * 2 + 15
BONF = 0.05 / N_TESTS
FINAL_SEEDS = list(range(21))


def _contrast(df, col, target, min_n=20):
    s = df[col].dropna()
    if len(s) < 500:
        return None
    lo_t, hi_t = s.quantile([0.3, 0.7])
    lo = df.loc[df[col] <= lo_t, target]
    hi = df.loc[df[col] >= hi_t, target]
    if len(lo) < min_n or len(hi) < min_n:
        return None
    return float(hi.mean() - lo.mean())


def _survey(df_main, folds, col, target, dq_th):
    s = df_main[col].dropna()
    if len(s) < 500:
        return {"feature": col, "g1": False}
    ic, p = stats.spearmanr(s, df_main.loc[s.index, target])
    try:
        b = pd.qcut(df_main[col], 5, duplicates="drop")
        g = df_main.groupby(b, observed=True)[target]
        dq = float(g.mean().iloc[-1] - g.mean().iloc[0])
    except ValueError:
        return {"feature": col, "g1": False}
    g1 = p < BONF and abs(dq) >= dq_th
    res = {"feature": col, "ic": round(float(ic), 4), "dq": round(dq, 4), "g1": g1}
    if not g1:
        return res
    d = 1 if (ic >= 0 and dq >= 0) or (ic < 0 and dq < 0) else -1
    # 方向以量级对比为准（ic 与 dq 符号不一致时 dq 优先——分位端更稳健）
    d = 1 if dq >= 0 else -1
    res["direction"] = d
    same = 0
    yd = {}
    for y in YEARS:
        c = _contrast(df_main[df_main["year"] == y], col, target)
        yd[y] = None if c is None else round(c, 3)
        if c is not None and np.sign(c) == d:
            same += 1
    res["year_same"], res["years"] = same, yd
    fd, g3 = {}, True
    for f in ("wf1_2020_21", "wf2_2022_23"):
        c = _contrast(folds[f], col, target)
        fd[f] = None if c is None else round(c, 3)
        if c is None or np.sign(c) != d:
            g3 = False
    res["folds"], res["g3"] = fd, g3
    res["survivor"] = bool(same >= 4 and g3)
    return res


def main():
    from discovery.split import Segment, extended_split

    df = pd.read_parquet(os.path.join(OUT_DIR, "trades_features.parquet"))
    main_df = df[df["pool"] == "main"].copy()
    main_df["occ"] = main_df["wait_days"] + main_df["holding_bars"]
    main_df["t_eff"] = main_df["avg_pnl_pct"] / main_df["occ"].clip(lower=1)
    folds = {}
    for f in ("wf1_2020_21", "wf2_2022_23"):
        ff = df[(df["pool"] == f) & (~df["embargoed"])].copy()
        ff["occ"] = ff["wait_days"] + ff["holding_bars"]
        ff["t_eff"] = ff["avg_pnl_pct"] / ff["occ"].clip(lower=1)
        folds[f] = ff
    print(f"[prereg] 目标 t_eff= pnl/槽天（主）/t_occ（副）；18 特征+15 交互；"
          f"Bonferroni p<{BONF:.1e}；量级 ≥{MIN_EFF_DQ}pp/槽天；四闸见脚本头",
          flush=True)
    print(f"[data] 主池 {len(main_df)} 笔，occupancy 均值 "
          f"{main_df['occ'].mean():.1f} 天，t_eff 均值 {main_df['t_eff'].mean():.3f}",
          flush=True)

    # —— Stage 1：主/副目标普查 ——
    res_eff = [_survey(main_df, folds, c, "t_eff", MIN_EFF_DQ) for c in AT_SIGNAL]
    res_occ = [_survey(main_df, folds, c, "occ", MIN_OCC_DQ) for c in AT_SIGNAL]
    g1_eff = [r for r in res_eff if r.get("g1")]
    surv_eff = [r for r in res_eff if r.get("survivor")]
    surv_occ = [r for r in res_occ if r.get("survivor")]
    print(f"[stage1] t_eff: G1 过 {len(g1_eff)}/18，存活 {len(surv_eff)}："
          f"{[r['feature'] for r in surv_eff]}", flush=True)
    print(f"[stage1] t_occ: 存活 {len(surv_occ)}："
          f"{[r['feature'] for r in surv_occ]}", flush=True)

    # —— 交互（主目标，top6 两两，共线去重）——
    inter = []
    if g1_eff:
        cand = sorted([r for r in g1_eff], key=lambda x: -abs(x["ic"]))
        kept = []
        for r in cand:
            if any(abs(main_df[r["feature"]].corr(main_df[k["feature"]],
                                                  method="spearman")) > 0.95
                   for k in kept):
                continue
            kept.append(r)
            if len(kept) >= 6:
                break
        z = {c["feature"]: (main_df[c["feature"]] - main_df[c["feature"]].mean())
             / main_df[c["feature"]].std() for c in kept
             if main_df[c["feature"]].std() > 0}
        for a, b in combinations(list(z), 2):
            main_df[f"{a}*{b}"] = z[a] * z[b]
            for f, ff in folds.items():
                za = (ff[a] - ff[a].mean()) / ff[a].std()
                zb = (ff[b] - ff[b].mean()) / ff[b].std()
                ff[f"{a}*{b}"] = za * zb
                folds[f] = ff
            inter.append(_survey(main_df, folds, f"{a}*{b}", "t_eff", MIN_EFF_DQ))
        surv_inter = [r for r in inter if r.get("survivor")]
        print(f"[stage1] 交互存活 {len(surv_inter)}："
              f"{[r['feature'] for r in surv_inter]}", flush=True)
        surv_eff += surv_inter

    out = {"stage1_eff": res_eff, "stage1_occ": res_occ, "stage1_inter": inter,
           "surv_eff": [r["feature"] for r in surv_eff],
           "surv_occ": [r["feature"] for r in surv_occ]}

    # —— Stage 2：受控 A/B ——
    ab = {}
    if surv_eff or surv_occ:
        import pyarrow.parquet as pq
        _dates = pq.read_table("data_lake/a_shares_daily.parquet",
                               columns=["date"]).column("date").to_pandas()
        udates = pd.DatetimeIndex(sorted(_dates.unique()))
        udates = udates[udates >= pd.Timestamp("2021-01-01")]
        split = extended_split()
        inner, outer = split.inner, split.outer
        seg_full = Segment("full", date(2021, 1, 1), date(2026, 12, 31))
        train = main_df[(main_df["year"] >= 2022) & (main_df["year"] <= 2024)]

        filled_base = _to_filled(main_df)

        def run_variant(name, feats_dirs):
            score = vector_score(main_df, feats_dirs, train)
            d2 = main_df.copy()
            d2["priority"] = score
            filled = _to_filled(d2)
            pri_map = dict(zip(zip(d2["symbol"], d2["signal_date"]), d2["priority"]))
            for t in filled:
                t["priority"] = pri_map[(t["symbol"], t["signal_date"])]
            pm_pri = _pm(queue_order="priority")
            out = {}
            for seg_name, seg in (("inner", inner), ("outer", outer),
                                  ("full", seg_full)):
                fseg = [t for t in filled if seg.covers(t["signal_date"])]
                bseg = [t for t in filled_base if seg.covers(t["signal_date"])]
                pri_m = _ann(fseg, seg, udates, pm_pri)
                rnd_ms = [_ann(bseg, seg, udates, _pm(queue_order="random",
                                                      queue_seed=s))
                          for s in FINAL_SEEDS]
                rnd_anns = [m["ann"] for m in rnd_ms]
                rnd_med = float(np.median(rnd_anns))
                out[seg_name] = {
                    "pri": {"ann": round(pri_m["ann"], 4),
                            "n_taken": pri_m["n_taken"],
                            "win": pri_m["win"]},
                    "rnd_med": round(rnd_med, 4),
                    "beats": sum(1 for a in rnd_anns if pri_m["ann"] > a),
                    "delta_med": round(pri_m["ann"] - rnd_med, 4),
                    "rnd_taken_med": float(np.median([m["n_taken"] for m in rnd_ms])),
                }
            yearly = {}
            for y in (2021, 2022, 2023, 2024):
                seg = Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
                fseg = [t for t in filled if seg.covers(t["signal_date"])]
                bseg = [t for t in filled_base if seg.covers(t["signal_date"])]
                pri = _ann(fseg, seg, udates, pm_pri)["ann"]
                rmed = float(np.median([_ann(bseg, seg, udates,
                                             _pm(queue_order="random",
                                                 queue_seed=s))["ann"]
                                        for s in FINAL_SEEDS]))
                yearly[y] = {"pri": round(pri, 4), "rnd_med": round(rmed, 4),
                             "delta": round(pri - rmed, 4)}
            out["yearly_inner"] = yearly
            a1 = (out["inner"]["pri"]["ann"] > out["inner"]["rnd_med"]
                  and out["inner"]["beats"] >= 15)
            a2 = (out["outer"]["pri"]["ann"] > out["outer"]["rnd_med"]
                  and out["outer"]["beats"] >= 15)
            a3 = all(v["delta"] >= -0.10 for v in yearly.values())
            a4 = (out["inner"]["pri"]["n_taken"]
                  >= 0.7 * out["inner"]["rnd_taken_med"])
            out["gates"] = {"A1_inner": a1, "A2_outer_holdout": a2,
                            "A3_yearly": a3, "A4_taken": a4}
            out["verdict"] = "PASS" if (a1 and a2 and a3 and a4) else "VETO"
            print(f"[AB/{name}] inner Δ{out['inner']['delta_med']:+.4f}"
                  f"(胜{out['inner']['beats']}/21) outer "
                  f"Δ{out['outer']['delta_med']:+.4f}(胜{out['outer']['beats']}/21)"
                  f" → {out['verdict']}", flush=True)
            return out

        if surv_eff:
            dirs = {r["feature"]: r["direction"] for r in surv_eff}
            ab["eff_primary"] = {"feats": dirs, "res": run_variant("eff", dirs)}
        if surv_occ:
            dirs = {r["feature"]: -1 for r in surv_occ}   # 占用短者优先
            ab["occ_secondary"] = {"feats": dirs, "res": run_variant("occ", dirs)}
    else:
        print("[stage2] 无存活特征——H-R7d 无原料，null 收档", flush=True)
    out["stage2"] = ab
    out["verdict_primary"] = (ab.get("eff_primary", {}).get("res", {})
                              .get("verdict", "NO_MATERIAL"))

    with open(os.path.join(OUT_DIR, "r7d_throughput.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(f"[done] {OUT_DIR}/r7d_throughput.json "
          f"verdict={out['verdict_primary']}", flush=True)


if __name__ == "__main__":
    main()
