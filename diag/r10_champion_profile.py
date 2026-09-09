# -*- coding: utf-8 -*-
"""R10 · champion 画像 v2——修正：用 R9Runner 应用过滤维（与 loop 逐位同源）；
单位修正：avg_pnl_pct 已是百分数（round(p*100,2)），打印不再 ×100。"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd

from diag.r9_breadth_loop import BASE_LIQUIDITY, _ann, _pm, _seg, build_universe
from diag.r10_global_opt_loop import split_trial
from discovery.split import extended_split


def main():
    tp = json.load(open("logs/r10_global_opt/verify_best_params.json",
                        encoding="utf-8"))
    params, state = split_trial(tp)
    print(f"champion state: liq={state['liq']} filters={state['filters']}",
          flush=True)

    lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                           filters=[("date", ">=", pd.Timestamp("2021-01-01"))])
    full_idx = pd.read_parquet("data_lake/a_shares_daily.parquet",
                               columns=[]).index
    first_dates = pd.Series(full_idx.get_level_values("date"),
                            index=full_idx.get_level_values("symbol"),
                            dtype="object").groupby(level=0).min()
    first_dates = {k: pd.Timestamp(v) for k, v in first_dates.items()}
    del full_idx
    uni = build_universe(lake, BASE_LIQUIDITY)
    udates = next(iter(uni.values())).index
    split = extended_split()
    del uni

    from diag.r10_global_opt_loop import R10Runner
    R = R10Runner(params, lake, udates, split, first_dates)

    filled = R.filled_of(state)          # 与 loop 逐位同源（含过滤）
    inners = [r for r in filled
              if split.inner.covers(pd.Timestamp(r["signal_date"]))]
    pnls = [r["avg_pnl_pct"] for r in inners]   # 已是百分数（如 2.8 = +2.8%）
    med_ann = float(np.median([_ann(inners, split.inner, udates, _pm(seed=s))["ann"]
                               for s in range(5)]))
    full_ann = _ann(filled, _seg("full", date(2021, 1, 1), date(2026, 12, 31)),
                    udates, _pm())["ann"]
    n_taken = float(np.median([_ann(inners, split.inner, udates, _pm(seed=s))["n_taken"]
                               for s in range(5)]))
    print(f"\n=== champion（带过滤，loop 同源口径）===", flush=True)
    print(f"inner deployable ann 中位(K=5): {med_ann:+.4f} | oracle 全期 ann: "
          f"{full_ann:+.4f} | n_taken 中位: {n_taken:.0f}", flush=True)
    print(f"单笔(inner): n={len(pnls)} | 均值 {np.mean(pnls):+.2f}% | "
          f"中位 {np.median(pnls):+.2f}% | "
          f"胜率 {sum(p > 0 for p in pnls)/len(pnls)*100:.1f}%", flush=True)
    yearly = {}
    for y in (2021, 2022, 2023, 2024):
        seg = _seg(f"y{y}", date(y, 1, 1), date(y, 12, 31))
        yearly[y] = round(float(np.median(
            [_ann(inners, seg, udates, _pm(seed=s))["ann"]
             for s in range(5)])), 4)
    print(f"分年 deployable 中位: {yearly}", flush=True)


if __name__ == "__main__":
    main()
