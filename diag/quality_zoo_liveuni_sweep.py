# -*- coding: utf-8 -*-
"""信号缩减 · 实盘 universe 约束重筛（2026-08-29 · amihud 回退后的 Phase A）。

背景：amihud 全市场线在 live universe（创业板/科创板 300 只）全灭信号、板内
相对线四闸全灭——教训=池上验证不迁移。本筛**全部在 universe 子集上**（回测库
15,456 笔 universe 信号，logs/retro_subset_liveuni.parquet）。

预登记（先于运行写死）：
  候选 8 因子 × 削减档 {20%, 30%, 40%}：
    gk_vol60 / pk_vol60 / tail_spread60（OHLC 波动族——策略内存即有数据，
    部署零管道优先）；idio_vol60 / lpm60；amihud60（对照）；wic_pb（估值）；
    elg_pos_share20（资金流）
  削减语义：**当日信号池内分位**（每 signal_date 的信号按因子值排序，剔方向
  劣侧 X%；NaN 放行）——纯 EOD 可知、无跨日阈值、无外部截面依赖。
  四闸（同 signal_cutdown）：G1 outer 中位 Δ≥−0.01 ∧ G2 全期 Δ≥−0.01 ∧
    G3 逐年 Δ≥−0.02 ∧ G4 taken 降幅≤10%；基线=子集同口径。
  排序：过闸者按削减幅度降序、同幅取 outer Δ 高者。
  多重比较披露：24 格三连筛（zoo→cutdown→本筛）第三遍，任何过闸者标注
  「需 Phase B 时间外（史前窗 universe 子集）确认后才可谈部署」。

用法：PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_zoo_liveuni_sweep.py
产物：logs/quality/factor_zoo/liveuni_cutdown.{json,md}
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
SUBSET = "logs/retro_subset_liveuni.parquet"
LAKE = "data_lake/a_shares_daily.parquet"
SEEDS = list(range(21))
YEARS = [2022, 2023, 2024, 2025, 2026]
LEVELS = [0.20, 0.30, 0.40]
FACTORS = ["gk_vol60", "pk_vol60", "tail_spread60", "idio_vol60", "lpm60",
           "amihud60", "wic_pb", "elg_pos_share20"]
DIRECTION = {"gk_vol60": -1, "pk_vol60": -1, "tail_spread60": -1,
             "idio_vol60": -1, "lpm60": -1, "amihud60": +1, "wic_pb": -1,
             "elg_pos_share20": -1}
T0 = time.time()


def _log(m):
    print(f"[{time.time() - T0:>5.0f}s] {m}", flush=True)


def _pm():
    return PositionModel(capital=1_000_000, lot_size=100, min_fee=5.0,
                         max_positions=4, pos_cap=0.075, freeze_pending=True)


def med(filled, seg, udates, pm):
    ms = [portfolio_metrics(filled, seg, udates,
                            position_model=replace(pm, queue_order="random",
                                                   queue_seed=s)) for s in SEEDS]
    anns = [m["ann"] for m in ms]
    m_ = float(np.median(anns))
    return round(m_, 4), ms[int(np.argmin([abs(a - m_) for a in anns]))]["n_taken"]


def to_filled(df):
    return [{"symbol": r["symbol"], "signal_date": pd.Timestamp(r["signal_date"]),
             "buy_date": pd.Timestamp(r["buy_date"]),
             "entry_date": pd.Timestamp(r["buy_date"]),
             "exit_date": pd.Timestamp(r["exit_date"]),
             "avg_pnl_pct": r["avg_pnl_pct"], "entry": r["entry"],
             "entry_price": r["entry"]} for _, r in df.iterrows()]


def main():
    sub = pd.read_parquet(SUBSET)
    sub["signal_date"] = pd.to_datetime(sub["signal_date"])
    _log(f"universe 子集 {len(sub)} 笔")

    _log("compute factors")
    w = _matrix(LAKE, ["open", "high", "low", "close", "amount"],
                pd.Timestamp("2019-09-01"))
    open_, high, low = w["open"].astype("float32"), w["high"].astype("float32"), \
        w["low"].astype("float32")
    close, amount = w["close"].astype("float32"), w["amount"].astype("float32")
    ret = close.pct_change(fill_method=None).astype("float32")
    log_hl = np.log(high / low).astype("float32") ** 2
    log_co = np.log(close / open_).astype("float32") ** 2
    rs = np.log(high / close) * np.log(high / open_) \
        + np.log(low / close) * np.log(low / open_)
    feats = {
        "gk_vol60": np.sqrt((0.5 * log_hl - (2 * np.log(2) - 1) * log_co)
                            .rolling(60, min_periods=48).mean()).astype("float32"),
        "pk_vol60": np.sqrt(log_hl.rolling(60, min_periods=48).mean()
                            / (4 * np.log(2))).astype("float32"),
        "tail_spread60": (ret.rolling(60, min_periods=48).quantile(0.95)
                          - ret.rolling(60, min_periods=48).quantile(0.05)
                          ).astype("float32"),
        "lpm60": np.sqrt((ret.clip(upper=0) ** 2).astype("float32")
                         .rolling(60, min_periods=48).mean()).astype("float32"),
        "amihud60": (ret.abs() / (amount + 1.0)).astype("float32") \
            .rolling(60, min_periods=48).mean().astype("float32"),
    }
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
    feats["idio_vol60"] = (ret - ind_wide).rolling(60, min_periods=48) \
        .std().astype("float32")
    db = _matrix("data_lake/daily_basic.parquet", ["pb"],
                 pd.Timestamp("2019-09-01"))["pb"] \
        .reindex(index=close.index, columns=close.columns)
    feats["wic_pb"] = db.rank(axis=1, pct=True).astype("float32")
    try:
        mfb = _matrix("data_lake/moneyflow.parquet",
                      ["buy_elg_amount", "sell_elg_amount"],
                      pd.Timestamp("2019-09-01"))
        elg = (mfb["buy_elg_amount"] - mfb["sell_elg_amount"]) \
            .reindex(index=close.index, columns=close.columns)
        feats["elg_pos_share20"] = (elg > 0).astype("float32").rolling(
            20, min_periods=15).mean().astype("float32")
    except Exception as e:
        _log(f"moneyflow 跳过: {e}")
        FACTORS.remove("elg_pos_share20")
    del w, open_, high, low, log_hl, log_co, rs, ind_wide, db, ret, amount

    for f in FACTORS:
        sub[f] = _lk(feats[f], sub["signal_date"], sub["symbol"])
    del feats
    _log("factors attached")

    _dates = pq.read_table(LAKE, columns=["date"]).column("date").to_pandas()
    udates = pd.DatetimeIndex(sorted(pd.to_datetime(_dates).unique()))
    udates = udates[udates >= pd.Timestamp("2021-01-01")]
    split = holdout_split()
    segs = {"outer": split.outer,
            "full": Segment("full", date(2021, 1, 1), date(2026, 12, 31))}
    for y in YEARS:
        segs[f"y{y}"] = Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
    pm = _pm()
    base = to_filled(sub)
    q0 = {k: med(base, s, udates, pm) for k, s in segs.items()}
    _log(f"Q0: { {k: v[0] for k, v in q0.items()} } taken={q0['outer'][1]}")

    # 当日信号池内分位（每日组内 rank pct）
    results = []
    for f in FACTORS:
        v = pd.to_numeric(sub[f], errors="coerce")
        r = pd.DataFrame({"g": sub["signal_date"].to_numpy(),
                          "v": v.to_numpy(dtype=float)})
        day_q = r.groupby("g")["v"].rank(pct=True).to_numpy()
        q_aligned = np.where(DIRECTION[f] > 0, day_q, 1.0 - day_q)  # 高=质量高
        for lv in LEVELS:
            keep = (q_aligned > lv) | ~np.isfinite(v.to_numpy(dtype=float))
            fdf = sub[keep].reset_index(drop=True)
            filled = to_filled(fdf)
            mo, tk = med(filled, segs["outer"], udates, pm)
            mf, _ = med(filled, segs["full"], udates, pm)
            yd, ok3 = {}, True
            if mo - q0["outer"][0] >= -0.01 and mf - q0["full"][0] >= -0.01 \
                    and (q0["outer"][1] - tk) <= 0.10 * q0["outer"][1]:
                for y in YEARS:
                    my, _ = med(filled, segs[f"y{y}"], udates, pm)
                    yd[y] = round(my - q0[f"y{y}"][0], 4)
                    if my - q0[f"y{y}"][0] < -0.02:
                        ok3 = False
            else:
                ok3 = False
                yd = {"note": "前置闸未过"}
            passed = bool(ok3 and yd.get("note") is None)
            results.append({"factor": f, "level": lv,
                            "cut_pct": round((1 - keep.mean()) * 100, 1),
                            "outer": mo, "d_outer": round(mo - q0["outer"][0], 4),
                            "full": mf, "d_full": round(mf - q0["full"][0], 4),
                            "taken": tk, "yearly": yd, "pass": passed})
            _log(f"[{f} −{int(lv*100)}%] cut {(1-keep.mean()):.0%} "
                 f"outer Δ{mo-q0['outer'][0]:+.4f} full Δ{mf-q0['full'][0]:+.4f} "
                 f"{'PASS' if passed else 'fail'}")

    passing = sorted([r for r in results if r["pass"]],
                     key=lambda r: (-r["level"], -r["d_outer"]))
    with open(os.path.join(OUT_DIR, "liveuni_cutdown.json"), "w",
              encoding="utf-8") as f:
        json.dump({"q0": {k: v[0] for k, v in q0.items()}, "grid": results,
                   "passing_sorted": passing,
                   "semantics": "当日信号池内分位剔方向劣侧；四闸同 signal_cutdown",
                   "caveat": "第三遍筛选（zoo→cutdown→本筛），过闸者须史前窗 universe 子集时间外确认",
                   "generated_at": pd.Timestamp.now().isoformat()},
                  f, ensure_ascii=False, indent=1, default=str)
    L = ["# 实盘 universe 约束信号缩减重筛（2026-08-29 Phase A）", "",
         f"> 子集 {len(sub)} 笔；语义=当日信号池分位；四闸预登记。",
         f"**过闸 {len(passing)} / {len(results)} 格**", "",
         "| 因子 | 削减 | Δouter | Δ全期 | 逐年Δ | taken |", "|---|---|---|---|---|---|"]
    for r in passing:
        L.append(f"| {r['factor']} | −{r['cut_pct']:.0f}% | "
                 f"{r['d_outer']:+.1%} | {r['d_full']:+.1%} | "
                 f"{r['yearly']} | {r['taken']} |")
    with open(os.path.join(OUT_DIR, "liveuni_cutdown.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    _log(f"[done] 过闸 {len(passing)}/{len(results)}；总 {(time.time()-T0)/60:.0f}min")


if __name__ == "__main__":
    main()
