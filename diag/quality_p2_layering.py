# -*- coding: utf-8 -*-
"""信号质量 P2 · 质量分层受控 A/B（2026-08-26 方案 Phase 2 · 本轮核心）。

中心假设：质量分不用于过滤（丢信号必伤收益——三案实证）而用于分层（弹性仓位）。
固定 7.5% vs 质量弹性 3%-10%（score 分位线性映射，4 并发不变）。

预登记五闸（先于看数写死于此，违反任何一条 = 否决）：
  ① 冻结口径 ann：质量臂 outer 2026 ann > 固定臂（对照线 ≈ +400%，数据尾前进
     则两臂同数据内部对比）；
  ② 逐年段无恶化：2022-2026 每年（冻结口径分段）ann 差 ≥ −0.02（>2pp 恶化即否）；
  ③ 2022 折改善：wf1 诚实池 oos 2022 上质量臂 ann > 固定臂；
  ④ 滑点 25bps 下增益存活：slippage_bps=25 时 outer ann 差 > 0；
  ⑤ 拥挤日不降级：taken 集中 same_day_n>80 的笔，质量臂均值 ≥ 固定臂 − 0.5pp。

质量分 v0（不用 ML）：
  - 特征 = P1 存活 ∩ at_signal 白名单（signal_date 收盘可知——P2 分层红线）；
  - 训练 2022-2024（时序切分，禁随机 CV）：z 标准化参数 + 分位 CDF 只取训练段；
  - 主型 = 方向对齐 z 等权平均；副型（敏感性）= 训练段 IC 加权；
  - 分位 p（对训练 CDF）→ pos_cap = 0.03 + 0.07p，clip[0.03, 0.10]；
    （线性映射均值 0.065 < 0.075 固定臂 = 保守设计：质量臂若胜出是顶着 ~13%
    平均敞口劣势胜出的；两臂敞口在报告并列。）

产物：logs/quality/layering_report.md + logs/quality/layering_result.json

用法：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_p2_layering.py
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

IN_PARQUET = "logs/quality/trades_features.parquet"
IN_SURVIVORS = "logs/quality/survivors.json"
OUT_DIR = "logs/quality"

AT_SIGNAL_OK = {"h_atr", "rr_id", "touches", "n_tops", "bottom_disp",
                "pattern_days", "neckline_span", "suppression", "ret5",
                "ret20", "ret60", "pos_high60", "breakout_ret", "atr_pct",
                "bvr", "vol5_slope", "pv_corr10", "same_day_n"}
TRAIN_YEARS = (2022, 2024)          # 含端
POS_MIN, POS_SPAN = 0.03, 0.07      # pos_cap = 0.03 + 0.07×p ∈ [0.03, 0.10]
NEUTRAL_PCT = 0.5
YEARS_GATE = [2022, 2023, 2024, 2025, 2026]


def _pm(**kw):
    from backtest.models import PositionModel
    base = dict(capital=1_000_000, lot_size=100, min_fee=5.0,
                max_positions=4, pos_cap=0.075, freeze_pending=True)
    base.update(kw)
    return PositionModel(**base)


def _to_filled(d):
    """特征行 → 流水 dict。

    双键契约：portfolio_metrics 读 "entry"（其内部转 entry_price），直接喂
    build_equity_curve（闸⑤ taken 集）读 "entry_price"——两键都带。
    """
    out = []
    for _, r in d.iterrows():
        t = {"symbol": r["symbol"], "signal_date": r["signal_date"],
             "buy_date": r["buy_date"], "entry_date": r["buy_date"],
             "exit_date": r["exit_date"],
             "avg_pnl_pct": r["avg_pnl_pct"], "entry": r["entry"],
             "entry_price": r["entry"], "same_day_n": r["same_day_n"]}
        if r.get("pos_cap") is not None and not pd.isna(r.get("pos_cap")):
            t["pos_cap"] = float(r["pos_cap"])
        out.append(t)
    return out


def _ann(filled, seg, udates, pm):
    from discovery.objective import portfolio_metrics
    m = portfolio_metrics(filled, seg, udates, position_model=pm)
    return {"ann": round(m["ann"], 4), "n_taken": m["n_taken"],
            "win": round(m["win_rate"], 3), "max_dd": round(m["max_dd"], 4),
            "n": m["n"]}


def _taken(filled, pm):
    from backtest.models import build_equity_curve
    curve, taken = build_equity_curve(filled, pm, return_taken=True)
    return taken


def build_scorer(train_df, feats):
    """训练段 z 参数 + 方向 + CDF。返回 (score_fn(row_dict)->pct, ic_weights)。"""
    from scipy import stats as _st
    zparams, dirs, ics = {}, {}, {}
    for f, d in feats.items():
        s = train_df[f].astype(float).dropna()
        mu, sd = float(s.mean()), float(s.std())
        if sd <= 0 or len(s) < 200:
            continue
        zparams[f] = (mu, sd)
        dirs[f] = d
        ic, _ = _st.spearmanr(train_df[f], train_df["avg_pnl_pct"],
                              nan_policy="omit")
        ics[f] = float(ic) if not np.isnan(ic) else 0.0

    def _score(row, weights=None):
        zs = []
        for f, (mu, sd) in zparams.items():
            v = row.get(f)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                continue
            z = (float(v) - mu) / sd * dirs[f]
            w = 1.0 if weights is None else weights[f]
            zs.append(z * w)
        if not zs:
            return None
        return float(np.mean(zs)) if weights is None else float(np.sum(zs))

    # 训练段分数样本（等权型）→ CDF
    train_scores = [_score(r) for _, r in train_df.iterrows()]
    train_scores = np.array([x for x in train_scores if x is not None])
    train_scores.sort()

    def _pct(score):
        if score is None:
            return NEUTRAL_PCT
        return float(np.searchsorted(train_scores, score, side="right")
                     / len(train_scores))

    return _score, _pct, zparams, ics


def apply_scores(df, feats, mode="equal", train_df=None):
    """打分 + 分位 + pos_cap。

    train_df 显式传入（默认取 df 自身训练年段）——wf 折臂必须传主池训练段：
    折 OOS 段自身做训练=违反时序切分预登记（折内 2022 年数据不可既当训练又当验证）。
    标准化参数/分位 CDF 全部来自训练段，其余段只用参数不回馈。
    """
    if train_df is None:
        train_df = df[(df["year"] >= TRAIN_YEARS[0]) & (df["year"] <= TRAIN_YEARS[1])]
    _score, _pct, zparams, ics = build_scorer(train_df, feats)
    weights = None
    if mode == "ic":
        tot = sum(abs(v) for v in ics.values()) or 1.0
        weights = {f: v / tot for f, v in ics.items()}
    out = df.copy()
    pcts, caps = [], []
    for _, r in out.iterrows():
        p = _pct(_score(r, weights))
        pcts.append(p)
        caps.append(min(POS_MIN + POS_SPAN * p, 0.10))
    out["score_pct"] = pcts
    out["pos_cap"] = caps
    return out, zparams, ics


def main():
    from discovery.split import Segment, holdout_split

    df = pd.read_parquet(IN_PARQUET)
    surv = json.load(open(IN_SURVIVORS, encoding="utf-8"))
    feats = {s["feature"]: s["direction"] for s in surv["survivors"]
             if s["feature"] in AT_SIGNAL_OK}
    L = []
    ap = L.append
    ap("# 质量分层受控 A/B 报告（P2 · 2026-08-26）")
    ap("")
    if not feats:
        ap("**P1 无 at_signal 存活特征 → P2 无原料，中心假设不可检验。**"
           "转 P3 分歧台账路径（不过闸分支）。")
        with open(os.path.join(OUT_DIR, "layering_report.md"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(L))
        json.dump({"verdict": "no_material"}, open(
            os.path.join(OUT_DIR, "layering_result.json"), "w",
            encoding="utf-8"), ensure_ascii=False, indent=1)
        print("[P2] 无存活特征，收档", flush=True)
        return
    ap(f"分层特征（P1 存活 ∩ at_signal）：{feats}")
    ap("")
    ap("预登记五闸：① 冻结口径 ann↑ ② 逐年段无恶化>2pp ③ wf2022 折改善 "
       "④ 滑点 25bps 增益存活 ⑤ 拥挤日(同日>80)不降级。映射 "
       f"pos_cap=0.03+0.07×p∈[0.03,0.10]，训练段 {TRAIN_YEARS[0]}-{TRAIN_YEARS[1]}。")
    ap("")

    main_df = df[df["pool"] == "main"].copy()
    fold1 = df[(df["pool"] == "wf1_2020_21") & (~df["embargoed"])].copy()
    # 训练段 = 主池 2022-2024（时序切分红线）：两臂与折臂共用同一份训练参数
    train_main = main_df[(main_df["year"] >= TRAIN_YEARS[0])
                         & (main_df["year"] <= TRAIN_YEARS[1])]
    # 完美共线去重（预登记，P1 同款）：|ρ|>0.95 的存活特征只留一个（按 P1 |IC|
    # 序——feats dict 保持 P1 顺序即 |IC| 序），防同一表征在等权分里双重计权
    _ks = list(feats)
    for f in list(_ks):
        for g in _ks:
            if g == f or f not in feats or g not in feats:
                continue
            rho = main_df[f].corr(main_df[g], method="spearman")
            if rho is not None and abs(rho) > 0.95:
                del feats[g]
                _ks = list(feats)
                break
    print(f"[P2] 分层特征（共线去重后 {len(feats)} 个）：{feats}", flush=True)

    # 市场日历（n_days 分母）：湖 date 列（索引元数据，pyarrow 直读）
    import pyarrow.parquet as pq
    _dates = pq.read_table("data_lake/a_shares_daily.parquet",
                           columns=["date"]).column("date").to_pandas()
    udates = pd.DatetimeIndex(sorted(_dates.unique()))
    udates = udates[udates >= pd.Timestamp("2021-01-01")]

    split = holdout_split()
    outer = split.outer
    seg_full = Segment("full", date(2021, 1, 1), date(2026, 12, 31))
    seg22 = Segment("y2022", date(2022, 1, 1), date(2022, 12, 31))

    results = {"features": feats, "mode_primary": "equal", "gates": {}}
    dfq_by_mode = {}
    for mode in ("equal", "ic"):
        dfq, zparams, ics = apply_scores(main_df, feats, mode=mode,
                                         train_df=train_main)
        dfq_by_mode[mode] = dfq
        fixed = _to_filled(main_df)
        quality = _to_filled(dfq)
        pm_f, pm_q = _pm(), _pm(quality_alloc=True)

        # ① 冻结口径 ann（outer 2026）
        f_out = _ann([t for t in fixed if outer.covers(t["signal_date"])],
                     outer, udates, pm_f)
        q_out = _ann([t for t in quality if outer.covers(t["signal_date"])],
                     outer, udates, pm_q)
        g1 = q_out["ann"] > f_out["ann"]

        # ② 逐年段无恶化
        yearly = {}
        for y in YEARS_GATE:
            seg = Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
            fy = _ann([t for t in fixed if seg.covers(t["signal_date"])],
                      seg, udates, pm_f)
            qy = _ann([t for t in quality if seg.covers(t["signal_date"])],
                      seg, udates, pm_q)
            yearly[y] = {"fixed": fy, "quality": qy,
                         "delta": round(qy["ann"] - fy["ann"], 4)}
        g2 = all(v["delta"] >= -0.02 for v in yearly.values())

        # ③ wf1 oos 2022 折（训练参数来自主池 train_main，折内零回馈）
        fold_q, _, _ = apply_scores(fold1, feats, mode=mode, train_df=train_main)
        f_f1 = _ann(_to_filled(fold1), seg22, udates, pm_f)
        q_f1 = _ann(_to_filled(fold_q), seg22, udates, pm_q)
        g3 = q_f1["ann"] > f_f1["ann"]

        # ④ 滑点 25bps
        f25 = _ann([t for t in fixed if outer.covers(t["signal_date"])],
                   outer, udates, _pm(slippage_bps=25))
        q25 = _ann([t for t in quality if outer.covers(t["signal_date"])],
                   outer, udates, _pm(slippage_bps=25, quality_alloc=True))
        g4 = q25["ann"] > f25["ann"]

        # ⑤ 拥挤日不降级（taken 集 same_day_n>80）
        taken_f = _taken([t for t in fixed if outer.covers(t["signal_date"])], pm_f)
        taken_q = _taken([t for t in quality if outer.covers(t["signal_date"])], pm_q)
        mf = [t["avg_pnl_pct"] for t in taken_f if t["same_day_n"] > 80]
        mq = [t["avg_pnl_pct"] for t in taken_q if t["same_day_n"] > 80]
        m_f = round(float(np.mean(mf)), 2) if mf else None
        m_q = round(float(np.mean(mq)), 2) if mq else None
        g5 = (m_f is not None and m_q is not None
              and m_q >= m_f - 0.5)

        gates = {"g1_ann_up": {"fixed": f_out, "quality": q_out,
                               "delta": round(q_out["ann"] - f_out["ann"], 4),
                               "pass": g1},
                 "g2_yearly": {y: v for y, v in yearly.items()} | {"pass": g2},
                 "g3_wf2022": {"fixed": f_f1, "quality": q_f1,
                               "delta": round(q_f1["ann"] - f_f1["ann"], 4),
                               "pass": g3},
                 "g4_slip25": {"fixed": f25, "quality": q25,
                               "delta": round(q25["ann"] - f25["ann"], 4),
                               "pass": g4},
                 "g5_crowded": {"fixed_mean": m_f, "quality_mean": m_q,
                                "n_f": len(mf), "n_q": len(mq), "pass": g5}}
        all_pass = g1 and g2 and g3 and g4 and g5
        results[mode] = {
            "gates": gates,
            "verdict": "PASS" if all_pass else "VETO",
            "exposure": {
                "mean_pos_cap_taken": round(float(np.mean(
                    [t["pos_cap"] for t in quality if "pos_cap" in t])), 4),
                "n_taken_outer": {"fixed": f_out["n_taken"],
                                  "quality": q_out["n_taken"]},
            },
            "zparams": {k: [round(v[0], 4), round(v[1], 4)]
                        for k, v in zparams.items()},
            "train_ics": {k: round(v, 4) for k, v in ics.items()},
        }
        print(f"[{mode}] ①{g1:d} ②{g2:d} ③{g3:d} ④{g4:d} ⑤{g5:d} → "
              f"{'PASS' if all_pass else 'VETO'} "
              f"(outer Δ{q_out['ann'] - f_out['ann']:+.3f})", flush=True)

    primary = results["equal"]
    verdict = primary["verdict"]
    results["verdict_primary"] = verdict

    # —— 事后机理性敏感性（非闸非采纳，仅入 P3 分歧台账）：均值匹配映射 ——
    # pos_cap = 0.075 + 0.07×(p−0.5) clip[0.03,0.10]：平均敞口 ≈0.0745≈固定臂。
    # 区分否决机制：纯敞口水平劣势（此变体追平）vs 倾斜本身有害（此变体仍输）。
    dfq_eq = dfq_by_mode["equal"]
    dfq_mm = dfq_eq.copy()
    dfq_mm["pos_cap"] = (0.075 + 0.07 * (dfq_mm["score_pct"] - 0.5)).clip(0.03, 0.10)
    fixed = _to_filled(main_df)
    quality_mm = _to_filled(dfq_mm)
    f_out_mm = _ann([t for t in fixed if outer.covers(t["signal_date"])],
                    outer, udates, _pm())
    q_out_mm = _ann([t for t in quality_mm if outer.covers(t["signal_date"])],
                    outer, udates, _pm(quality_alloc=True))
    yearly_mm = {}
    for y in YEARS_GATE:
        seg = Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
        fy = _ann([t for t in fixed if seg.covers(t["signal_date"])], seg, udates, _pm())
        qy = _ann([t for t in quality_mm if seg.covers(t["signal_date"])],
                  seg, udates, _pm(quality_alloc=True))
        yearly_mm[y] = round(qy["ann"] - fy["ann"], 4)
    results["mean_matched_sensitivity"] = {
        "map": "0.075+0.07*(p-0.5) clip[0.03,0.10]",
        "outer_fixed": f_out_mm, "outer_quality": q_out_mm,
        "delta_outer": round(q_out_mm["ann"] - f_out_mm["ann"], 4),
        "delta_yearly": yearly_mm,
        "mean_pos_cap": round(float(dfq_mm["pos_cap"].mean()), 4),
        "note": "事后机理分析（VETO 后），非预登记闸、不构成采纳依据",
    }
    print(f"[mm-sens] 外层 Δ{q_out_mm['ann'] - f_out_mm['ann']:+.3f} "
          f"(均pos_cap={dfq_mm['pos_cap'].mean():.4f})", flush=True)

    # —— 报告 ——
    ap(f"## 裁决：**{verdict}**（主型=等权；IC 加权副型见 json）")
    ap("")
    g = primary["gates"]
    ap("| 闸 | 固定臂 | 质量臂 | Δ | 过 |")
    ap("|---|---|---|---|---|")
    ap(f"| ① 外层 ann | {g['g1_ann_up']['fixed']['ann']:+.1%} | "
       f"{g['g1_ann_up']['quality']['ann']:+.1%} | "
       f"{g['g1_ann_up']['delta']:+.3f} | {'✅' if g['g1_ann_up']['pass'] else '❌'} |")
    ap(f"| ④ 滑点25bps ann | {g['g4_slip25']['fixed']['ann']:+.1%} | "
       f"{g['g4_slip25']['quality']['ann']:+.1%} | "
       f"{g['g4_slip25']['delta']:+.3f} | {'✅' if g['g4_slip25']['pass'] else '❌'} |")
    ap(f"| ③ wf2022 折 ann | {g['g3_wf2022']['fixed']['ann']:+.1%} | "
       f"{g['g3_wf2022']['quality']['ann']:+.1%} | "
       f"{g['g3_wf2022']['delta']:+.3f} | {'✅' if g['g3_wf2022']['pass'] else '❌'} |")
    ap(f"| ⑤ 拥挤日均值 | {g['g5_crowded']['fixed_mean']} | "
       f"{g['g5_crowded']['quality_mean']} | "
       f"{(g['g5_crowded']['quality_mean'] or 0) - (g['g5_crowded']['fixed_mean'] or 0):+.2f}pp | "
       f"{'✅' if g['g5_crowded']['pass'] else '❌'} |")
    ap("")
    mm = results["mean_matched_sensitivity"]
    ap(f"## 事后机理性敏感性（非闸非采纳）：均值匹配映射 {mm['map']}")
    ap("")
    ap(f"平均 pos_cap={mm['mean_pos_cap']}（≈固定臂 0.075）下外层 ann：固定 "
       f"{mm['outer_fixed']['ann']:+.1%} vs 质量 {mm['outer_quality']['ann']:+.1%}"
       f"（Δ{mm['delta_outer']:+.3f}）；逐年 Δ：{mm['delta_yearly']}。"
       "——区分「死在敞口水平」vs「死在倾斜本身」，结论入 P3 分歧台账。")
    ap("")
    ap("### ② 逐年段（冻结口径分段 ann）")
    ap("")
    ap("| 年 | 固定 | 质量 | Δ |")
    ap("|---|---|---|---|")
    for y in YEARS_GATE:
        v = g["g2_yearly"][str(y)] if str(y) in g["g2_yearly"] else g["g2_yearly"][y]
        ap(f"| {y} | {v['fixed']['ann']:+.1%} | {v['quality']['ann']:+.1%} | "
           f"{v['delta']:+.3f} |")
    ap("")
    exp = primary["exposure"]
    ap(f"敞口：质量臂 taken 平均 pos_cap={exp['mean_pos_cap_taken']:.3f} "
       f"（固定臂 0.075）；外层 taken {exp['n_taken_outer']}。")
    ap("")
    if verdict == "VETO":
        ap("**否决收档**：P3 走分歧台账路径（不过闸分支），分歧点登记见 P3 交接文档。")
    else:
        ap("**过闸**：P3 进入 QMT 侧弹性仓位工程 + 5 交易日双轨质量复盘。")
    ap("")
    with open(os.path.join(OUT_DIR, "layering_report.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(L))
    json.dump(results, open(os.path.join(OUT_DIR, "layering_result.json"),
                            "w", encoding="utf-8"), ensure_ascii=False,
              indent=1, default=str)
    print(f"[done] {OUT_DIR}/layering_report.md verdict={verdict}", flush=True)


if __name__ == "__main__":
    main()
