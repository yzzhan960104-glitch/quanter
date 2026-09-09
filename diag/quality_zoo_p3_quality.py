# -*- coding: utf-8 -*-
"""因子家族淘汰赛 · Phase 3：质量轴分层 A/B（2026-08-29 · 方案定稿执行）。

预登记（先于运行写死）：
  口径：PM 同 P2（1e6/整手/min5/4×0.075/freeze）；**两臂同用可部署口径**
  （random 21 种子中位——较 P2 原版 A/B 的 exit_date 先知序是诚实度升级）。
  臂：
    Q0 基线：固定 0.075；
    Q1 主臂：idio_vol60（P1 时间外存活替补规则产出），方向 −1（低特异波动=
      高质量位）→ 同日截面 pct 秩 q，质量位 p=1−q；
    Q2 副臂：amihud20，方向 +1 → p=q。
  映射（均值匹配+严格敞口归一）：cap = 0.075+0.07×(p−0.5) clip[0.03,0.10]，
  段内重标定 c=0.075/mean(caps) 后再 clip 一次；NaN 特征→0.075 中性仓。
  闸：① outer 中位 ann↑；② 逐年 Δ≥−0.02；③ wf2022 折；④ 滑点 25bps
  （Q2 追加 50bps）；⑤ 拥挤日（各自中位种子 taken）；⑥ 敞口审计
  |段内实际均帽−0.075|≤0.001（任一段违规=作废）。

用法：PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_zoo_p3_quality.py
产物：logs/quality/factor_zoo/p3_quality.{json,md}
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


def caps_for(df, feat, direction):
    """段内均值匹配仓位序列（NaN→0.075）。返回 (caps, 实际均帽)。"""
    v = pd.to_numeric(df[feat], errors="coerce").to_numpy(dtype=float)
    d = pd.DataFrame({"g": df["signal_date"].to_numpy(), "v": v})
    q = d.groupby("g")["v"].rank(pct=True).to_numpy()
    p = q if direction > 0 else 1.0 - q
    caps = 0.075 + 0.07 * (p - 0.5)
    caps = np.where(np.isfinite(v), caps, 0.075)
    ok = np.isfinite(v)
    if ok.sum() > 100:
        c = 0.075 / float(np.clip(caps[ok], 0.03, 0.10).mean())
        caps = np.where(ok, np.clip(caps * c, 0.03, 0.10), 0.075)
    return caps, float(np.mean(caps))


def to_filled(df, caps=None):
    out = []
    for i, (_, r) in enumerate(df.iterrows()):
        t = {"symbol": r["symbol"], "signal_date": r["signal_date"],
             "buy_date": r["buy_date"], "entry_date": r["buy_date"],
             "exit_date": r["exit_date"], "avg_pnl_pct": r["avg_pnl_pct"],
             "entry": r["entry"], "entry_price": r["entry"],
             "same_day_n": r["same_day_n"]}
        if caps is not None:
            t["pos_cap"] = float(caps[i])
        out.append(t)
    return out


def med_ann(filled, seg, udates, pm):
    ms = [portfolio_metrics(filled, seg, udates,
                            position_model=replace(pm, queue_order="random",
                                                   queue_seed=s))
          for s in SEEDS]
    anns = [m["ann"] for m in ms]
    med = float(np.median(anns))
    rep = SEEDS[int(np.argmin([abs(a - med) for a in anns]))]
    return med, rep, ms[rep]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    trades = pd.read_parquet(IN_MAIN)
    trades["signal_date"] = pd.to_datetime(trades["signal_date"])
    main_df = trades[trades["pool"] == "main"].copy().reset_index(drop=True)
    fold1 = trades[(trades["pool"] == "wf1_2020_21") & (~trades["embargoed"])] \
        .copy().reset_index(drop=True)

    _log("compute idio_vol60 / amihud20")
    close = _matrix(LAKE, ["close", "amount"], LOAD_FROM)
    cdf, amt = close["close"], close["amount"]
    ret = cdf.pct_change(fill_method=None).astype("float32")
    sb = pq.read_table("data_lake/stock_basic.parquet",
                       columns=["ts_code", "industry"]).to_pandas()
    ind_map = dict(zip(sb["ts_code"].astype(str), sb["industry"].astype(str)))
    ind_wide = pd.DataFrame(np.nan, index=cdf.index, columns=cdf.columns,
                            dtype="float32")
    groups = {}
    for sym in cdf.columns:
        groups.setdefault(ind_map.get(sym, None), []).append(sym)
    for name, cols in groups.items():
        if name is None or name != name:
            continue
        ser = ret[cols].mean(axis=1).astype("float32")
        ind_wide[cols] = np.repeat(ser.to_numpy()[:, None], len(cols), axis=1)
    idio = (ret - ind_wide).rolling(60, min_periods=48).std().astype("float32")
    amihud = (ret.abs() / (amt + 1.0)).astype("float32").rolling(
        20, min_periods=16).mean().astype("float32")
    del close, cdf, amt, ret, ind_wide
    for df in (main_df, fold1):
        df["idio_vol60"] = _lk(idio, df["signal_date"], df["symbol"])
        df["amihud20"] = _lk(amihud, df["signal_date"], df["symbol"])

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

    pm_fixed = _pm()
    pm_q = _pm(quality_alloc=True)

    def seg_df(name):
        return fold1 if name == "fold22" else \
            main_df[main_df["signal_date"].apply(segs[name].covers)] \
            .reset_index(drop=True)

    results = {}
    # —— Q0 基线（各段中位）——
    _log("Q0 fixed baseline")
    q0 = {}
    for name in segs:
        df = seg_df(name)
        med, rep, _ = med_ann(to_filled(df), segs[name], udates, pm_fixed)
        q0[name] = {"median_ann": round(med, 4), "rep_seed": rep}
        _log(f"  Q0 {name}: {med:+.4f}")
    q0_slip25 = med_ann(to_filled(seg_df("outer")), outer, udates,
                       replace(pm_fixed, slippage_bps=30.0))[0]
    q0_slip50 = med_ann(to_filled(seg_df("outer")), outer, udates,
                       replace(pm_fixed, slippage_bps=55.0))[0]

    # —— Q1 / Q2 ——
    for arm, feat, direction in (("Q1_idio_vol60", "idio_vol60", -1),
                                 ("Q2_amihud20", "amihud20", 1)):
        r = {"arm": arm, "feature": feat, "direction": direction}
        audit = {}
        filled_by_seg = {}
        for name in list(segs) + ["slip25", "slip50"]:
            if name in ("slip25", "slip50"):
                df = seg_df("outer")
                seg_obj = outer
            else:
                df = seg_df(name)
                seg_obj = segs[name]
            caps, actual_mean = caps_for(df, feat, direction)
            audit[name] = round(actual_mean, 5)
            filled_by_seg[name] = (df, caps, seg_obj)
        g6 = all(abs(v - 0.075) <= 0.001 for v in audit.values())
        r["exposure_audit"] = audit
        r["gates"] = {"g6_exposure": bool(g6)}

        def _med(name, pmx):
            df, caps, seg_obj = filled_by_seg[name]
            return med_ann(to_filled(df, caps), seg_obj, udates, pmx)

        med_out, rep_out, rep_m = _med("outer", pm_q)
        r["outer"] = {"median_ann": round(med_out, 4), "rep": rep_m}
        g1 = med_out > q0["outer"]["median_ann"]
        r["yearly"] = {}
        g2 = True
        for y in YEARS_GATE:
            my, _, _ = _med(f"y{y}", pm_q)
            r["yearly"][y] = round(my - q0[f"y{y}"]["median_ann"], 4)
            if my - q0[f"y{y}"]["median_ann"] < -0.02:
                g2 = False
        mf, _, _ = _med("fold22", pm_q)
        g3 = mf > q0["fold22"]["median_ann"]
        r["fold22"] = {"arm_med": round(mf, 4),
                       "q0_med": q0["fold22"]["median_ann"]}
        ms25, _, _ = _med("slip25", replace(pm_q, slippage_bps=30.0))
        g4 = ms25 > q0_slip25
        r["slip25"] = {"arm_med": round(ms25, 4),
                       "q0_med": round(q0_slip25, 4)}
        extra = {}
        if arm.startswith("Q2"):
            ms50, _, _ = _med("slip50", replace(pm_q, slippage_bps=55.0))
            extra = {"slip50_arm": round(ms50, 4),
                     "slip50_q0": round(q0_slip50, 4),
                     "pass": ms50 > q0_slip50}
        r["slip50_extra"] = extra
        # ⑤ 拥挤日（中位种子 taken）
        df, caps, seg_obj = filled_by_seg["outer"]
        fq = to_filled(df, caps)
        _, taken_q = build_equity_curve(
            fq, replace(pm_q, queue_order="random", queue_seed=rep_out),
            return_taken=True)
        df0 = seg_df("outer")
        f0 = to_filled(df0)
        _, taken0 = build_equity_curve(
            f0, replace(pm_fixed, queue_order="random",
                        queue_seed=q0["outer"]["rep_seed"]), return_taken=True)
        m0 = [t["avg_pnl_pct"] for t in taken0 if t["same_day_n"] > 80]
        mq = [t["avg_pnl_pct"] for t in taken_q if t["same_day_n"] > 80]
        mm0 = float(np.mean(m0)) if m0 else None
        mmq = float(np.mean(mq)) if mq else None
        g5 = (mm0 is not None and mmq is not None and mmq >= mm0 - 0.5)
        r["crowded"] = {"q0": round(mm0, 2) if mm0 else None,
                        "arm": round(mmq, 2) if mmq else None}
        full_med, _, _ = _med("full", pm_q)
        r["full_median_ann"] = round(full_med, 4)
        r["gates"].update({"g1": bool(g1), "g2": bool(g2), "g3": bool(g3),
                           "g4": bool(g4), "g5": bool(g5)})
        passed = g1 and g2 and g3 and g4 and g5 and g6
        if arm.startswith("Q2") and extra:
            passed = passed and extra["pass"]
        r["pass"] = bool(passed)
        results[arm] = r
        _log(f"[{arm}] ①{int(g1)} ②{int(g2)} ③{int(g3)} ④{int(g4)} "
             f"⑤{int(g5)} ⑥{int(g6)}"
             + (f" slip50:{int(extra['pass'])}" if extra else "")
             + f" → {'PASS' if r['pass'] else 'VETO'} "
             f"(outer {med_out:+.1%} vs Q0 {q0['outer']['median_ann']:+.1%})")

    champion = next((a for a in ("Q1_idio_vol60", "Q2_amihud20")
                     if results[a]["pass"]), None)
    out = {"q0": q0, "q0_slip25": round(q0_slip25, 4),
           "q0_slip50": round(q0_slip50, 4), "arms": results,
           "champion": champion,
           "note": "两臂同可部署口径（random 21 种子中位）；同日秩+段内均值匹配",
           "generated_at": pd.Timestamp.now().isoformat()}
    with open(os.path.join(OUT_DIR, "p3_quality.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    L = ["# Phase 3 · 质量轴分层 A/B（2026-08-29）", "",
         f"> Q0 outer 中位 {q0['outer']['median_ann']:+.1%}；冠军={champion}", ""]
    for arm, r in results.items():
        g = r["gates"]
        L += [f"## {arm} → {'**PASS**' if r['pass'] else '**VETO**'}", "",
              f"- ① outer 中位 {r['outer']['median_ann']:+.1%} vs "
              f"Q0 {q0['outer']['median_ann']:+.1%} {'✅' if g['g1'] else '❌'}；"
              f"全期中位 {r['full_median_ann']:+.1%}",
              f"- ② 逐年 Δ {r['yearly']} {'✅' if g['g2'] else '❌'}",
              f"- ③ wf2022 {r['fold22']['arm_med']:+.1%} vs "
              f"{r['fold22']['q0_med']:+.1%} {'✅' if g['g3'] else '❌'}",
              f"- ④ 滑点25 {r['slip25']['arm_med']:+.1%} vs "
              f"{r['slip25']['q0_med']:+.1%} {'✅' if g['g4'] else '❌'}"
              + (f"；追加50：{r['slip50_extra']}" if r["slip50_extra"] else ""),
              f"- ⑤ 拥挤日 arm {r['crowded']['arm']} vs "
              f"Q0 {r['crowded']['q0']} {'✅' if g['g5'] else '❌'}",
              f"- ⑥ 敞口审计 {r['exposure_audit']} "
              f"{'✅' if g['g6_exposure'] else '❌'}", ""]
    with open(os.path.join(OUT_DIR, "p3_quality.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    _log(f"[done] champion={champion} 总 {(time.time() - T0) / 60:.0f}min")


if __name__ == "__main__":
    main()
