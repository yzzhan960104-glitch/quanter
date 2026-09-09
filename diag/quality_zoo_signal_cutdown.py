# -*- coding: utf-8 -*-
"""信号缩减测试（2026-08-29 · 用户目标函数变更：收益持平下砍信号量）。

与既往裁决的关系：此前所有过滤/分层 VETO 的目标函数是「提高 ann」；本测试
目标函数=「max 信号削减 s.t. 收益不降」。机理依据（先于看数写死）：
  oracle 口径下过滤必死（量喂 ann——batch1 过滤测试全部 −0.3~−1.6）；
  可部署口径下 4 槽×0.075 是瓶颈、外层信号供给≈槽位 25 倍（11520 vs 445），
  剔除低质量侧预期不减少 taken（槽照常被填），只抬高随机抽签池的平均质量
  → 同收益少信号在机制上成立，本测试首次实证它。

预登记：
  口径：PM=1e6/整手/min5/4×0.075/freeze；可部署=random 21 种子中位。
  候选（质量轴登记册+batch1 真信号，方向沿用）×削减档：
    amihud60(+1) / idio_vol60(−1) / wic_pb(−1) / wic_amount(−1) /
    lpm60(−1) / composite_z(上四者等权同日 z，方向同上)
    × drop 低质量侧 {20%, 30%, 40%, 50%}（NaN 特征保留不剔）。
  四闸：
    G1 outer2026 中位 Δ ≥ −0.01（收益不降容差 1pp）
    G2 全期 2021-26 中位 Δ ≥ −0.01
    G3 逐年(22-26) 中位 Δ ≥ −0.02（各年）
    G4 taken 数（中位种子）降幅 ≤ 10%（保「砍信号」非「砍交易」）
  读数排序：过闸组合按削减幅度降序（用户目标=减量），同幅取 outer Δ 高者。

用法：PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_zoo_signal_cutdown.py
产物：logs/quality/factor_zoo/signal_cutdown.{md,json}
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

from backtest.models import PositionModel
from diag.quality_momentum_batch2 import _lk, _matrix
from discovery.objective import portfolio_metrics
from discovery.split import Segment, holdout_split

OUT_DIR = "logs/quality/factor_zoo"
IN_MAIN = "logs/quality/trades_features.parquet"
LAKE = "data_lake/a_shares_daily.parquet"
LOAD_FROM = pd.Timestamp("2019-09-01")
SEEDS = list(range(21))
YEARS_GATE = [2022, 2023, 2024, 2025, 2026]
LEVELS = [0.20, 0.30, 0.40, 0.50]
CANDS = {"amihud60": +1, "idio_vol60": -1, "wic_pb": -1, "wic_amount": -1,
         "lpm60": -1, "composite_z": None}
T0 = time.time()


def _log(m):
    print(f"[{time.time() - T0:>5.0f}s] {m}", flush=True)


def _pm(**kw):
    base = dict(capital=1_000_000, lot_size=100, min_fee=5.0, max_positions=4,
                pos_cap=0.075, freeze_pending=True)
    base.update(kw)
    return PositionModel(**base)


def to_filled(df):
    return [{"symbol": r["symbol"], "signal_date": r["signal_date"],
             "buy_date": r["buy_date"], "entry_date": r["buy_date"],
             "exit_date": r["exit_date"], "avg_pnl_pct": r["avg_pnl_pct"],
             "entry": r["entry"], "entry_price": r["entry"]}
            for _, r in df.iterrows()]


def med(filled, seg, udates, pm):
    ms = [portfolio_metrics(filled, seg, udates,
                            position_model=replace(pm, queue_order="random",
                                                   queue_seed=s))
          for s in SEEDS]
    anns = [m["ann"] for m in ms]
    med_ann = float(np.median(anns))
    rep = SEEDS[int(np.argmin([abs(a - med_ann) for a in anns]))]
    return {"med": round(med_ann, 4), "taken": ms[rep]["n_taken"],
            "n": ms[rep]["n"], "band": [round(min(anns), 4), round(max(anns), 4)]}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    trades = pd.read_parquet(IN_MAIN)
    trades["signal_date"] = pd.to_datetime(trades["signal_date"])
    main_df = trades[trades["pool"] == "main"].copy().reset_index(drop=True)

    _log("compute candidate features")
    w = _matrix(LAKE, ["close", "amount"], LOAD_FROM)
    close, amount = w["close"], w["amount"]
    ret = close.pct_change(fill_method=None).astype("float32")
    amihud60 = (ret.abs() / (amount + 1.0)).astype("float32").rolling(
        60, min_periods=48).mean().astype("float32")
    lpm60 = np.sqrt((ret.clip(upper=0) ** 2).astype("float32")
                    .rolling(60, min_periods=48).mean()).astype("float32")
    idio60 = None
    sb = pq.read_table("data_lake/stock_basic.parquet",
                       columns=["ts_code", "industry"]).to_pandas()
    ind_map = dict(zip(sb["ts_code"].astype(str), sb["industry"].astype(str)))
    ind_wide = pd.DataFrame(np.nan, index=close.index, columns=close.columns,
                            dtype="float32")
    groups = {}
    for sym in close.columns:
        groups.setdefault(ind_map.get(sym, None), []).append(sym)
    for name, cols in groups.items():
        if name is None or name != name:
            continue
        ser = ret[cols].mean(axis=1).astype("float32")
        ind_wide[cols] = np.repeat(ser.to_numpy()[:, None], len(cols), axis=1)
    idio60 = (ret - ind_wide).rolling(60, min_periods=48).std().astype("float32")
    db = _matrix("data_lake/daily_basic.parquet", ["pb"], LOAD_FROM)["pb"] \
        .reindex(index=close.index, columns=close.columns)
    wic_pb = db.rank(axis=1, pct=True).astype("float32")
    wic_amount = amount.rank(axis=1, pct=True).astype("float32")
    del w, close, amount, ret, ind_wide, db, groups

    td, ts = main_df["signal_date"], main_df["symbol"]
    main_df["amihud60"] = _lk(amihud60, td, ts)
    main_df["lpm60"] = _lk(lpm60, td, ts)
    main_df["idio_vol60"] = _lk(idio60, td, ts)
    main_df["wic_pb"] = _lk(wic_pb, td, ts)
    main_df["wic_amount"] = _lk(wic_amount, td, ts)
    # composite_z：四特征方向对齐后的同日截面 z 等权
    def _zw(col, d):
        v = main_df[col].to_numpy(dtype=float)
        r = pd.DataFrame({"g": td.to_numpy(), "v": v}).groupby("g")["v"] \
            .rank(pct=True).to_numpy()
        p = r if d > 0 else 1.0 - r
        return np.where(np.isfinite(v), p - 0.5, 0.0)
    main_df["composite_z"] = (_zw("amihud60", 1) + _zw("idio_vol60", -1)
                              + _zw("wic_pb", -1) + _zw("wic_amount", -1))
    CANDS["composite_z"] = +1     # composite 已方向对齐，高=质量高
    _log(f"features ready, main {len(main_df)}")

    _dates = pq.read_table(LAKE, columns=["date"]).column("date").to_pandas()
    udates = pd.DatetimeIndex(sorted(pd.to_datetime(_dates).unique()))
    udates = udates[udates >= pd.Timestamp("2021-01-01")]
    split = holdout_split()
    outer = split.outer
    seg_full = Segment("full", date(2021, 1, 1), date(2026, 12, 31))
    pm = _pm()

    # —— 基线 ——
    _log("Q0 baselines")
    base_filled = to_filled(main_df)
    q0 = {"outer": med(base_filled, outer, udates, pm),
          "full": med(base_filled, seg_full, udates, pm)}
    q0_yearly = {}
    for y in YEARS_GATE:
        seg = Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
        q0_yearly[y] = med(base_filled, seg, udates, pm)
        _log(f"  Q0 y{y}: {q0_yearly[y]['med']:+.4f} taken {q0_yearly[y]['taken']}")
    _log(f"Q0 outer {q0['outer']['med']:+.4f} (taken {q0['outer']['taken']}) "
         f"/ full {q0['full']['med']:+.4f}")

    # —— 组合网格（两段式：outer+full 先筛，过者补逐年）——
    results = []
    for cand, d in CANDS.items():
        v = main_df[cand].to_numpy(dtype=float)
        for lv in LEVELS:
            q = pd.Series(v).quantile(
                lv if d > 0 else 1 - lv)          # 低质量侧阈值
            keep_mask = (v > q) if d > 0 else (v < q)
            keep_mask |= ~np.isfinite(v)          # NaN 保留
            kept = main_df[keep_mask].reset_index(drop=True)
            cut = 1 - len(kept) / len(main_df)
            filled = to_filled(kept)
            mo = med(filled, outer, udates, pm)
            mf = med(filled, seg_full, udates, pm)
            g1 = mo["med"] - q0["outer"]["med"] >= -0.01
            g2 = mf["med"] - q0["full"]["med"] >= -0.01
            g4 = (q0["outer"]["taken"] - mo["taken"]) <= 0.10 * q0["outer"]["taken"]
            r = {"cand": cand, "level": lv, "cut_pct": round(cut * 100, 1),
                 "outer": mo, "full": mf,
                 "g1": bool(g1), "g2": bool(g2), "g4": bool(g4),
                 "yearly": None, "g3": None, "pass": False}
            if g1 and g2 and g4:
                yd = {}
                g3 = True
                for y in YEARS_GATE:
                    seg = Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
                    my = med(filled, seg, udates, pm)
                    yd[y] = round(my["med"] - q0_yearly[y]["med"], 4)
                    if my["med"] - q0_yearly[y]["med"] < -0.02:
                        g3 = False
                r["yearly"] = yd
                r["g3"] = bool(g3)
                r["pass"] = True
            results.append(r)
            _log(f"[{cand} −{int(lv*100)}%] outer {mo['med']:+.4f} "
                 f"(Δ{mo['med']-q0['outer']['med']:+.4f}) full "
                 f"{mf['med']:+.4f} (Δ{mf['med']-q0['full']['med']:+.4f}) "
                 f"taken {mo['taken']} → "
                 f"{'PASS' if r['pass'] else 'fail'}")

    passing = sorted([r for r in results if r["pass"]],
                     key=lambda r: (-r["level"], -(r["outer"]["med"]
                                                   - q0["outer"]["med"])))
    out = {"q0": q0, "q0_yearly": {y: v["med"] for y, v in q0_yearly.items()},
           "grid": results, "passing_sorted": passing,
           "gates": ["G1 outer Δ≥−0.01", "G2 full Δ≥−0.01",
                     "G3 yearly Δ≥−0.02 each", "G4 taken 降幅≤10%"],
           "objective": "max 信号削减 s.t. 可部署口径收益不降（非 max 收益）",
           "generated_at": pd.Timestamp.now().isoformat()}
    with open(os.path.join(OUT_DIR, "signal_cutdown.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    L = ["# 信号缩减测试（收益持平砍信号 · 2026-08-29）", "",
         f"> Q0：outer 中位 {q0['outer']['med']:+.1%} / 全期 "
         f"{q0['full']['med']:+.1%} / taken {q0['outer']['taken']}",
         f"> 闸：G1 outer Δ≥−0.01 ∧ G2 全期 Δ≥−0.01 ∧ G3 逐年 Δ≥−0.02 "
         "∧ G4 taken 降幅≤10%。排序=削减幅度优先。", "",
         f"**过闸组合 {len(passing)} 个**（全表见 json）", "",
         "| 候选 | 削减 | outer 中位 | Δouter | Δ全期 | 逐年Δ | taken |",
         "|---|---|---|---|---|---|---|"]
    for r in passing:
        L.append(f"| {r['cand']} | −{r['cut_pct']:.0f}% | "
                 f"{r['outer']['med']:+.1%} | "
                 f"{r['outer']['med']-q0['outer']['med']:+.1%} | "
                 f"{r['full']['med']-q0['full']['med']:+.1%} | "
                 f"{r['yearly']} | {r['outer']['taken']} |")
    with open(os.path.join(OUT_DIR, "signal_cutdown.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    _log(f"[done] 过闸 {len(passing)}；总 {(time.time() - T0) / 60:.0f}min")


if __name__ == "__main__":
    main()
