# -*- coding: utf-8 -*-
"""因子家族淘汰赛 · Phase 2：吞吐轴优先级 A/B（2026-08-29 · 方案定稿执行）。

预登记（先于运行写死）：
  口径：PM=1e6/整手100/min5/4 并发/pos_cap 0.075/freeze_pending；判定单位=
  可部署口径（queue_order="random" 21 种子中位，seed=range(21)）。
  臂（方向对齐 occupancy：priority = −dir×z，z=同日截面 pct 秩−0.5）：
    T0 基线：无优先级 random 中位（现任可部署序）；
    T1 几何簇：priority = z(suppression)+z(neck_rel)−z(risk_pct_v)
              （三特征 occ 方向 −1/−1/+1 → 期望占用短者优先）；
    T2 alpha60：priority = −z(alpha60)（dir+1=占用长，取反）；
    T3 kaufman_er60：priority = −z(kaufman_er60)（同上）。
  五闸：① outer2026 arm > T0 中位；② 逐年(22-26) Δ=arm−中位 ≥ −0.02；
    ③ wf1 oos2022 折 arm > T0 折中位；④ 滑点 25bps arm > T0 中位；
    ⑤ 拥挤日(同日>80) taken 均值 arm ≥ T0 代表种子 −0.5pp
      （T0 代表种子=outer ann 最接近其中位的种子）。
  冠军 = 过闸臂中 ①Δ 最高；T3 仅当 T1/T2 双灭才计参赛（方案预登记）。

用法：PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_zoo_p2_throughput.py
产物：logs/quality/factor_zoo/p2_throughput.{json,md}
"""
import json
import os
import sys
import time
from dataclasses import replace
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backtest.models import PositionModel, build_equity_curve
from diag.quality_momentum_batch2 import _lk, _matrix
from discovery.objective import portfolio_metrics
from discovery.split import Segment, holdout_split

OUT_DIR = "logs/quality/factor_zoo"
IN_MAIN = "logs/quality/trades_features.parquet"
LAKE = "data_lake/a_shares_daily.parquet"
LOAD_FROM = pd.Timestamp("2019-09-01")
SEEDS = list(range(21))
YEARS_GATE = [2022, 2023, 2024, 2025, 2026]
T0 = time.time()


def _log(m):
    print(f"[{time.time() - T0:>5.0f}s] {m}", flush=True)


def _pm(**kw):
    base = dict(capital=1_000_000, lot_size=100, min_fee=5.0, max_positions=4,
                pos_cap=0.075, freeze_pending=True)
    base.update(kw)
    return PositionModel(**base)


def _z_within(df, col):
    v = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
    d = pd.DataFrame({"g": df["signal_date"].to_numpy(), "v": v})
    r = d.groupby("g")["v"].rank(pct=True).to_numpy()
    return np.where(np.isfinite(v), r - 0.5, 0.0)


def _to_filled(df, priority=None):
    out = []
    for i, (_, r) in enumerate(df.iterrows()):
        t = {"symbol": r["symbol"], "signal_date": r["signal_date"],
             "buy_date": r["buy_date"], "entry_date": r["buy_date"],
             "exit_date": r["exit_date"], "avg_pnl_pct": r["avg_pnl_pct"],
             "entry": r["entry"], "entry_price": r["entry"],
             "same_day_n": r["same_day_n"]}
        if priority is not None:
            t["priority"] = float(priority[i])
        out.append(t)
    return out


def _median_random(filled, seg, udates, pm, slip=0.0, seeds=SEEDS):
    ms = [portfolio_metrics(filled, seg, udates,
                            position_model=replace(pm, queue_order="random",
                                                   queue_seed=s,
                                                   slippage_bps=5.0 + slip))
          for s in seeds]
    anns = [m["ann"] for m in ms]
    med = float(np.median(anns))
    rep_seed = seeds[int(np.argmin([abs(a - med) for a in anns]))]
    return {"median_ann": round(med, 4), "rep_seed": rep_seed,
            "band": [round(min(anns), 4), round(max(anns), 4)],
            "rep_metrics": ms[rep_seed]}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    trades = pd.read_parquet(IN_MAIN)
    trades["signal_date"] = pd.to_datetime(trades["signal_date"])
    main_df = trades[trades["pool"] == "main"].copy().reset_index(drop=True)
    fold1 = trades[(trades["pool"] == "wf1_2020_21") & (~trades["embargoed"])] \
        .copy().reset_index(drop=True)
    for df in (main_df, fold1):
        df["neck_rel"] = df["neckline"] / df["entry"]
        df["risk_pct_v"] = pd.to_numeric(df["risk_pct"], errors="coerce")

    # alpha60 / kaufman_er60 重算挂列
    _log("compute alpha60/kaufman_er60")
    close = _matrix(LAKE, ["close"], LOAD_FROM)["close"]
    ret = close.pct_change(fill_method=None).astype("float32")
    mkt = ret.mean(axis=1)
    xm = ret.mul(mkt, axis=0).rolling(250, min_periods=200).mean()
    rm_ = ret.rolling(250, min_periods=200).mean()
    mm_ = mkt.rolling(250, min_periods=200).mean()
    mv_ = mkt.rolling(250, min_periods=200).var()
    beta = xm.sub(rm_.mul(mm_, axis=0)).div(mv_, axis=0)
    alpha60 = (ret.rolling(60, min_periods=48).mean()
               - beta.mul(mkt.rolling(60, min_periods=48).mean(), axis=0)
               ).astype("float32")
    diff = close.diff().abs()
    kauf = ((close - close.shift(60)).abs()
            / (diff.rolling(60, min_periods=48).sum() + 1e-12)).astype("float32")
    del close, ret, mkt, xm, rm_, mm_, mv_, beta, diff
    for df in (main_df, fold1):
        df["alpha60"] = _lk(alpha60, df["signal_date"], df["symbol"])
        df["kaufman_er60"] = _lk(kauf, df["signal_date"], df["symbol"])
    _log(f"main {len(main_df)} / fold1 {len(fold1)}")

    _dates = pq.read_table(LAKE, columns=["date"]).column("date").to_pandas()
    udates = pd.DatetimeIndex(sorted(pd.to_datetime(_dates).unique()))
    udates = udates[udates >= pd.Timestamp("2021-01-01")]
    split = holdout_split()
    outer = split.outer
    segs = {"outer": outer}
    for y in YEARS_GATE:
        segs[f"y{y}"] = Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
    segs["fold22"] = Segment("f22", date(2022, 1, 1), date(2022, 12, 31))
    segs["full"] = Segment("full", date(2021, 1, 1), date(2026, 12, 31))

    pm = _pm()
    base_filled = _to_filled(main_df)

    # —— T0 基线（各段 21 种子中位）——
    _log("T0 baseline medians")
    t0 = {}
    for name, seg in segs.items():
        t0[name] = _median_random(base_filled, seg, udates, pm)
        _log(f"  T0 {name}: med {t0[name]['median_ann']}")
    t0_slip = _median_random(base_filled, outer, udates, pm, slip=25.0)

    # —— 臂定义（priority 数组）——
    def arm_priorities(df):
        z_sup = _z_within(df, "suppression")
        z_neck = _z_within(df, "neck_rel")
        z_risk = _z_within(df, "risk_pct_v")
        z_a60 = _z_within(df, "alpha60")
        z_kau = _z_within(df, "kaufman_er60")
        return {"T1_geometry": z_sup + z_neck - z_risk,
                "T2_alpha60": -z_a60,
                "T3_kaufman_er60": -z_kau}

    pr_main = arm_priorities(main_df)
    pr_fold = arm_priorities(fold1)
    results = {}
    for arm, pr in pr_main.items():
        filled = _to_filled(main_df, pr)
        pm_arm = replace(pm, queue_order="priority")
        r = {"arm": arm}
        r["outer"] = portfolio_metrics(filled, outer, udates,
                                       position_model=pm_arm)
        g1 = r["outer"]["ann"] > t0["outer"]["median_ann"]
        r["yearly"] = {}
        g2 = True
        for y in YEARS_GATE:
            seg = segs[f"y{y}"]
            ay = portfolio_metrics(filled, seg, udates, position_model=pm_arm)
            t0y = t0[f"y{y}"]["median_ann"]
            r["yearly"][y] = {"arm": round(ay["ann"], 4),
                              "t0_med": t0y,
                              "delta": round(ay["ann"] - t0y, 4)}
            if ay["ann"] - t0y < -0.02:
                g2 = False
        ff = _to_filled(fold1, pr_fold[arm])
        r["fold22"] = {"arm": portfolio_metrics(ff, segs["fold22"], udates,
                                                position_model=pm_arm),
                       "t0_med": t0["fold22"]["median_ann"]}
        g3 = r["fold22"]["arm"]["ann"] > t0["fold22"]["median_ann"]
        r["slip25"] = {"arm": portfolio_metrics(
            filled, outer, udates,
            position_model=replace(pm_arm, slippage_bps=30.0)),
            "t0_med": t0_slip["median_ann"]}
        g4 = r["slip25"]["arm"]["ann"] > t0_slip["median_ann"]
        # ⑤ 拥挤日：T0 代表种子 taken vs arm taken
        rep = replace(pm, queue_order="random", queue_seed=t0["outer"]["rep_seed"])
        _, taken0 = build_equity_curve(base_filled, rep, return_taken=True)
        _, takenA = build_equity_curve(filled, pm_arm, return_taken=True)
        m0 = [t["avg_pnl_pct"] for t in taken0 if t["same_day_n"] > 80]
        mA = [t["avg_pnl_pct"] for t in takenA if t["same_day_n"] > 80]
        mm0 = float(np.mean(m0)) if m0 else None
        mmA = float(np.mean(mA)) if mA else None
        g5 = (mm0 is not None and mmA is not None and mmA >= mm0 - 0.5)
        r["crowded"] = {"t0_rep": round(mm0, 2) if mm0 else None,
                        "arm": round(mmA, 2) if mmA else None,
                        "n_t0": len(m0), "n_arm": len(mA)}
        r["full"] = portfolio_metrics(filled, segs["full"], udates,
                                      position_model=pm_arm)
        r["gates"] = {"g1": bool(g1), "g2": bool(g2), "g3": bool(g3),
                      "g4": bool(g4), "g5": bool(g5)}
        r["pass"] = bool(g1 and g2 and g3 and g4 and g5)
        results[arm] = r
        _log(f"[{arm}] ①{int(g1)} ②{int(g2)} ③{int(g3)} ④{int(g4)} ⑤{int(g5)}"
             f" → {'PASS' if r['pass'] else 'VETO'} "
             f"(outer arm {r['outer']['ann']:+.1%} vs T0 "
             f"{t0['outer']['median_ann']:+.1%})")

    # 冠军裁定：过闸臂中 ①Δ 最高；T3 仅当 T1/T2 双灭
    passing = [a for a in ("T1_geometry", "T2_alpha60")
               if results[a]["pass"]]
    if not passing and results["T3_kaufman_er60"]["pass"]:
        passing = ["T3_kaufman_er60"]
    champion = max(passing, key=lambda a: results[a]["outer"]["ann"]
                   - t0["outer"]["median_ann"]) if passing else None

    out = {"t0": t0, "t0_slip25": t0_slip, "arms": results,
           "champion": champion,
           "note": "可部署口径（random 21 种子中位基线）；priority=−dir×z(同日截面秩)",
           "generated_at": pd.Timestamp.now().isoformat()}
    with open(os.path.join(OUT_DIR, "p2_throughput.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    L = ["# Phase 2 · 吞吐轴优先级 A/B（2026-08-29）", "",
         f"> T0=可部署 random 21 种子中位（outer "
         f"{t0['outer']['median_ann']:+.1%}）；五闸预登记；冠军={champion}", ""]
    for arm, r in results.items():
        g = r["gates"]
        L.append(f"## {arm} → {'**PASS**' if r['pass'] else '**VETO**'}")
        L.append("")
        L.append(f"- ① outer：arm {r['outer']['ann']:+.1%} vs T0 "
                 f"{t0['outer']['median_ann']:+.1%}"
                 f"（Δ{r['outer']['ann'] - t0['outer']['median_ann']:+.3f}）"
                 f" {'✅' if g['g1'] else '❌'}；"
                 f"全期 arm {r['full']['ann']:+.1%}")
        L.append(f"- ② 逐年 Δ：{ {y: v['delta'] for y, v in r['yearly'].items()} }"
                 f" {'✅' if g['g2'] else '❌'}")
        L.append(f"- ③ wf2022：arm {r['fold22']['arm']['ann']:+.1%} vs "
                 f"T0 {r['fold22']['t0_med']:+.1%} {'✅' if g['g3'] else '❌'}")
        L.append(f"- ④ 滑点25：arm {r['slip25']['arm']['ann']:+.1%} vs "
                 f"T0 {r['slip25']['t0_med']:+.1%} {'✅' if g['g4'] else '❌'}")
        L.append(f"- ⑤ 拥挤日：arm {r['crowded']['arm']} vs "
                 f"T0 {r['crowded']['t0_rep']}（n={r['crowded']['n_arm']}/"
                 f"{r['crowded']['n_t0']}）{'✅' if g['g5'] else '❌'}")
        L.append("")
    with open(os.path.join(OUT_DIR, "p2_throughput.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    _log(f"[done] champion={champion} 总 {(time.time() - T0) / 60:.0f}min")


if __name__ == "__main__":
    main()
