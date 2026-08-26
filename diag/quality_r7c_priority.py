# -*- coding: utf-8 -*-
"""H-R7c · 排队优先级重排受控测试（2026-08-26 用户指令「保留 H-R7a，先探索 H-R7c」）。

════════════════════════════════════════════════════════════════════
预登记协议（先于看数写死——本脚本作者此刻未见过任何 priority_queue 读数）
════════════════════════════════════════════════════════════════════
机制（P2 闸⑤发现的选股面）：冻结核下 taken 集由占用顺序决定、与仓位大小解耦；
同日候选按引擎惯例序（occupy_from, exit_date——注意 exit_date 是模拟约定非前视
外泄，历史全部读数同款）无差别进场，信号过剩（80+/日 vs 4 槽）下 ~96% 候选被
任意丢弃。假设：按质量分重排同日进场优先级（高质先得槽）改善冻结核组合口径。

受测对象（冻结，不重选）：
  特征 = survivors.json 的 at_signal 存活集（bottom_disp/vol5_slope，方向 +1）；
  分数 = 方向对齐 z 等权平均（训练段 2022-2024 池内标准化，向量实现同
  build_scorer 的逐笔可得均值语义）；NaN 特征跳过、全缺记 0 中性；
  重排 = PositionModel.priority_queue=True（同 occupy_from 内 priority 降序）；
  对照 = 引擎原序（所有历史读数的口径）；两臂均固定 7.5% 仓位、同 PM——
  唯一差异是同日候选的进场顺序。

样本：主池 = 创板科创（部署域，主裁决）；副池 = 全市场（H-R7a 同款
trades_features_fullmkt，机制压力测试——信号过剩更大）。训练段各池内时序取。

六闸（每池独立判，全过=确认；任一否=翻号）：
  ① 外层 2026 冻结口径 ann：priority > baseline；
  ② 逐年段 2022-2026：Δ ≥ −0.02/年；
  ③ 全期 2021-2026 ann：priority > baseline（防外层单年侥幸）；
  ④ 滑点 25bps 外层：priority > baseline；
  ⑤ taken 数不塌：外层 |Δn_taken|/n_baseline ≤ 10%（重排只换人不砍量）；
  ⑥ 拥挤日（same_day_n>80）：priority taken 均值 ≥ baseline − 0.5pp
     （机制主闸——拥挤日正是重排起作用的地方）。
诊断（非闸）：外层 taken 集变化比例（重排咬合度）、两臂 taken 交集。
主裁决 = 创板科创池等权型。
════════════════════════════════════════════════════════════════════

用法：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_r7c_priority.py
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from diag.quality_p2_layering import _ann, _pm, _taken

OUT_DIR = "logs/quality"
YEARS_GATE = (2022, 2023, 2024, 2025, 2026)


def vector_score(df, feats, train):
    """方向对齐 z 等权分（NaN 跳过、全缺 NaN→填 0 中性）。同 build_scorer 语义。"""
    zsum = pd.Series(0.0, index=df.index)
    zcnt = pd.Series(0.0, index=df.index)
    for f, d in feats.items():
        mu = float(train[f].astype(float).mean())
        sd = float(train[f].astype(float).std())
        if sd <= 0:
            continue
        z = (df[f].astype(float) - mu) / sd * d
        zsum = zsum.add(z.fillna(0.0))
        zcnt = zcnt.add(z.notna().astype(float))
    score = zsum / zcnt.replace(0.0, np.nan)
    return score.fillna(0.0)


def _to_filled_p(d, with_priority=False):
    out = []
    for _, r in d.iterrows():
        t = {"symbol": r["symbol"], "signal_date": r["signal_date"],
             "buy_date": r["buy_date"], "entry_date": r["buy_date"],
             "exit_date": r["exit_date"],
             "avg_pnl_pct": r["avg_pnl_pct"], "entry": r["entry"],
             "entry_price": r["entry"], "same_day_n": r["same_day_n"]}
        if with_priority:
            t["priority"] = float(r["priority"])
        out.append(t)
    return out


def run_pool(tag, df, feats, udates, split, seg_full):
    outer = split.outer
    train = df[(df["year"] >= 2022) & (df["year"] <= 2024)]
    df = df.copy()
    df["priority"] = vector_score(df, feats, train)
    base_filled = _to_filled_p(df, with_priority=False)
    prio_filled = _to_filled_p(df, with_priority=True)
    pm_b, pm_p = _pm(), _pm(priority_queue=True)

    f_out = _ann([t for t in base_filled if outer.covers(t["signal_date"])],
                 outer, udates, pm_b)
    p_out = _ann([t for t in prio_filled if outer.covers(t["signal_date"])],
                 outer, udates, pm_p)
    g1 = p_out["ann"] > f_out["ann"]
    f_full = _ann(base_filled, seg_full, udates, pm_b)
    p_full = _ann(prio_filled, seg_full, udates, pm_p)
    g3 = p_full["ann"] > f_full["ann"]
    f25 = _ann([t for t in base_filled if outer.covers(t["signal_date"])],
               outer, udates, _pm(slippage_bps=25))
    p25 = _ann([t for t in prio_filled if outer.covers(t["signal_date"])],
               outer, udates, _pm(slippage_bps=25, priority_queue=True))
    g4 = p25["ann"] > f25["ann"]

    yearly, g2 = {}, True
    for y in YEARS_GATE:
        seg = Segment_y(y)
        fy = _ann([t for t in base_filled if seg.covers(t["signal_date"])],
                  seg, udates, pm_b)
        py = _ann([t for t in prio_filled if seg.covers(t["signal_date"])],
                  seg, udates, pm_p)
        yearly[y] = {"fixed": fy, "priority": py,
                     "delta": round(py["ann"] - fy["ann"], 4)}
        if yearly[y]["delta"] < -0.02:
            g2 = False

    g5 = abs(p_out["n_taken"] - f_out["n_taken"]) / max(f_out["n_taken"], 1) <= 0.10

    taken_b = _taken([t for t in base_filled if outer.covers(t["signal_date"])], pm_b)
    taken_p = _taken([t for t in prio_filled if outer.covers(t["signal_date"])], pm_p)
    mb = [t["avg_pnl_pct"] for t in taken_b if t["same_day_n"] > 80]
    mp = [t["avg_pnl_pct"] for t in taken_p if t["same_day_n"] > 80]
    m_b = round(float(np.mean(mb)), 2) if mb else None
    m_p = round(float(np.mean(mp)), 2) if mp else None
    g6 = m_b is not None and m_p is not None and m_p >= m_b - 0.5

    keys_b = {(t["symbol"], t["signal_date"]) for t in taken_b}
    keys_p = {(t["symbol"], t["signal_date"]) for t in taken_p}
    changed = 1.0 - len(keys_b & keys_p) / max(len(keys_b | keys_p), 1)
    verdict = "PASS" if (g1 and g2 and g3 and g4 and g5 and g6) else "VETO"
    res = {"pool": tag, "n": int(len(df)),
           "g1_outer": {"base": f_out, "priority": p_out,
                        "delta": round(p_out["ann"] - f_out["ann"], 4), "pass": g1},
           "g2_yearly": yearly | {"pass": g2},
           "g3_full": {"base": f_full, "priority": p_full,
                       "delta": round(p_full["ann"] - f_full["ann"], 4), "pass": g3},
           "g4_slip25": {"base": f25, "priority": p25,
                         "delta": round(p25["ann"] - f25["ann"], 4), "pass": g4},
           "g5_taken_count": {"base": f_out["n_taken"], "priority": p_out["n_taken"],
                              "pass": g5},
           "g6_crowded": {"base_mean": m_b, "priority_mean": m_p,
                          "n_b": len(mb), "n_p": len(mp), "pass": g6},
           "taken_changed_frac": round(changed, 3),
           "verdict": verdict}
    print(f"[{tag}] ①{int(g1)} ②{int(g2)} ③{int(g3)} ④{int(g4)} ⑤{int(g5)} "
          f"⑥{int(g6)} → {verdict}（外层Δ{p_out['ann'] - f_out['ann']:+.3f} "
          f"全期Δ{p_full['ann'] - f_full['ann']:+.3f} taken换手{changed:.0%} "
          f"拥挤日 {m_b}->{m_p}）", flush=True)
    return res


def Segment_y(y):
    from discovery.split import Segment
    return Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))


def main():
    from discovery.split import Segment, holdout_split

    surv = json.load(open(os.path.join(OUT_DIR, "survivors.json"),
                          encoding="utf-8"))
    feats = {s["feature"]: s["direction"] for s in surv["survivors"]
             if s["feature"] in ("bottom_disp", "vol5_slope")}
    assert set(feats) == {"bottom_disp", "vol5_slope"}, f"特征冻结域被破坏：{feats}"
    print(f"[prereg] 特征冻结：{feats}；重排=priority_queue 高分先得槽；"
          f"两臂同固定 7.5%；六闸见脚本头", flush=True)

    import pyarrow.parquet as pq
    _dates = pq.read_table("data_lake/a_shares_daily.parquet",
                           columns=["date"]).column("date").to_pandas()
    udates = pd.DatetimeIndex(sorted(_dates.unique()))
    udates = udates[udates >= pd.Timestamp("2021-01-01")]
    split = holdout_split()
    seg_full = Segment("full", date(2021, 1, 1), date(2026, 12, 31))

    results = {"feats": feats, "pools": {}}
    ck = pd.read_parquet(os.path.join(OUT_DIR, "trades_features.parquet"))
    results["pools"]["chuangke"] = run_pool(
        "chuangke", ck[ck["pool"] == "main"].copy(), feats, udates, split, seg_full)
    fm = pd.read_parquet(os.path.join(OUT_DIR, "trades_features_fullmkt.parquet"))
    results["pools"]["fullmkt"] = run_pool(
        "fullmkt", fm, feats, udates, split, seg_full)
    results["verdict_primary"] = results["pools"]["chuangke"]["verdict"]

    with open(os.path.join(OUT_DIR, "r7c_priority.json"), "w",
              encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1, default=str)
    print(f"[done] {OUT_DIR}/r7c_priority.json "
          f"verdict(chuangke)={results['verdict_primary']}", flush=True)


if __name__ == "__main__":
    main()
