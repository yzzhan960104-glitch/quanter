# -*- coding: utf-8 -*-
"""双口径基线快照（2026-08-26 用户裁决 A「双口径并报」的落地产物）。

对 ACTIVE 冠军（B3 neckline_r610_b3_20260826）产出两口径的正式读数：
  oracle      = exit_date 同日序（历史先知口径，全部历史读数的连续性锚）；
  deployable  = symbol 同日序（实盘计划序代理，无未来信息）——对外声明标准。
并附 R7a 质量倾斜（bottom_disp+vol5_slope × 均值匹配映射）在可部署序下的
信息性读数（其 +26.9pp 确认系先知口径；部署前置重查项 B 的第一份数据）。

产物：logs/quality/dual_caliber_baseline.json（冠军换代后重跑本脚本即刷新）。

用法：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_dual_caliber_baseline.py
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from diag.quality_p2_layering import _ann, _pm, _to_filled, apply_scores
from diag.quality_r7c_priority import vector_score, _to_filled_p

OUT_DIR = "logs/quality"


def main():
    from discovery.objective import portfolio_metrics_dual
    from discovery.split import Segment, holdout_split

    df = pd.read_parquet(os.path.join(OUT_DIR, "trades_features.parquet"))
    df = df[df["pool"] == "main"].copy()

    import pyarrow.parquet as pq
    _dates = pq.read_table("data_lake/a_shares_daily.parquet",
                           columns=["date"]).column("date").to_pandas()
    udates = pd.DatetimeIndex(sorted(_dates.unique()))
    udates = udates[udates >= pd.Timestamp("2021-01-01")]

    split = holdout_split()
    outer = split.outer
    seg_full = Segment("full", date(2021, 1, 1), date(2026, 12, 31))

    def _dual(filled, seg):
        r = portfolio_metrics_dual(filled, seg, udates, position_model=_pm())
        dep = {k: round(v, 4) if isinstance(v, float) else v
               for k, v in r["deployable"].items() if not k.endswith("_band")}
        return {"oracle": r["oracle"], "deployable": dep,
                "ann_band": r["deployable"]["ann_band"]}

    filled = _to_filled(df)
    out = {
        "champion": "neckline_r610_b3_20260826",
        "note": ("oracle=exit_date 同日序（先知，历史连续性锚，仅内部）；"
                 "deployable=symbol 序（实盘计划序代理，对外声明标准）；"
                 "同冻结 PM（4×7.5% 整手+min5+freeze_pending@100w）"),
        "outer_2026": _dual([t for t in filled if outer.covers(t["signal_date"])], outer),
        "full_2021_26": _dual(filled, seg_full),
        "yearly": {},
    }
    for y in (2021, 2022, 2023, 2024, 2025, 2026):
        seg = Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
        out["yearly"][y] = _dual([t for t in filled if seg.covers(t["signal_date"])], seg)

    # —— R7a 倾斜在可部署序下的信息性读数（裁决点 B 的第一份数据，非新测试）——
    # 逐种子差值取中位数（比「中位数之差」更连贯），附极差。
    surv = json.load(open(os.path.join(OUT_DIR, "survivors.json"), encoding="utf-8"))
    feats = {s["feature"]: s["direction"] for s in surv["survivors"]
             if s["feature"] in ("bottom_disp", "vol5_slope")}
    train = df[(df["year"] >= 2022) & (df["year"] <= 2024)]
    dfq, _, _ = apply_scores(df, feats, mode="equal", train_df=train)
    dfq = dfq.copy()
    dfq["pos_cap"] = (0.075 + 0.07 * (dfq["score_pct"] - 0.5)).clip(0.03, 0.10)
    quality = _to_filled(dfq)
    fo = [t for t in filled if outer.covers(t["signal_date"])]
    qo = [t for t in quality if outer.covers(t["signal_date"])]
    deltas_out, deltas_full = [], []
    for s in range(21):
        d_o = (_ann(qo, outer, udates,
                    _pm(queue_order="random", queue_seed=s, quality_alloc=True))
               ["ann"]
               - _ann(fo, outer, udates, _pm(queue_order="random", queue_seed=s))
               ["ann"])
        d_f = (_ann(quality, seg_full, udates,
                    _pm(queue_order="random", queue_seed=s, quality_alloc=True))
               ["ann"]
               - _ann(filled, seg_full, udates,
                      _pm(queue_order="random", queue_seed=s))["ann"])
        deltas_out.append(round(d_o, 4))
        deltas_full.append(round(d_f, 4))
    out["r7a_tilt_under_deployable"] = {
        "note": ("信息性：R7a 确认 +26.9pp 系先知口径；此处为可部署随机序下同款"
                 "映射，逐种子 Δann 的中位数/极差"),
        "outer_delta_median": float(np.median(deltas_out)),
        "outer_delta_band": [min(deltas_out), max(deltas_out)],
        "full_delta_median": float(np.median(deltas_full)),
        "full_delta_band": [min(deltas_full), max(deltas_full)],
    }

    with open(os.path.join(OUT_DIR, "dual_caliber_baseline.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)

    print(f"[冠军·外层2026] oracle {out['outer_2026']['oracle']['ann']:+.1%}"
          f" / deployable(中位) {out['outer_2026']['deployable']['ann']:+.1%}"
          f"（taken {out['outer_2026']['oracle']['n_taken']}"
          f"/{out['outer_2026']['deployable']['n_taken']:.0f}，"
          f"ann 极差 {out['outer_2026']['ann_band'][0]:+.1%}"
          f"~{out['outer_2026']['ann_band'][1]:+.1%}）", flush=True)
    print(f"[冠军·全期]   oracle {out['full_2021_26']['oracle']['ann']:+.1%}"
          f" / deployable(中位) {out['full_2021_26']['deployable']['ann']:+.1%}"
          f"（taken {out['full_2021_26']['oracle']['n_taken']}"
          f"/{out['full_2021_26']['deployable']['n_taken']:.0f}）", flush=True)
    yr_d = {y: round(v["deployable"]["ann"], 3) for y, v in out["yearly"].items()}
    print(f"[冠军·可部署逐年(中位)] {yr_d}", flush=True)
    rt = out["r7a_tilt_under_deployable"]
    print(f"[R7a倾斜·可部署序] 外层Δ中位{rt['outer_delta_median']:+.3f}"
          f"（极差{rt['outer_delta_band'][0]:+.3f}~{rt['outer_delta_band'][1]:+.3f}）"
          f" 全期Δ中位{rt['full_delta_median']:+.3f}"
          f"（极差{rt['full_delta_band'][0]:+.3f}~{rt['full_delta_band'][1]:+.3f}）",
          flush=True)
    print(f"[done] {OUT_DIR}/dual_caliber_baseline.json", flush=True)


if __name__ == "__main__":
    main()
