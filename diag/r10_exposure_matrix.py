# -*- coding: utf-8 -*-
"""R10 · 敞口敏感性矩阵（用户 CAP.txt 决策参考，ADR-16 用户裁决域——只供数）。

champion 组合（verify best params+过滤）在 max_positions ∈ {4,8,13}
（占用 30%/60%/≈100%）下的 deployable 读数（K=5 种子中位），附供给端
信号池并发分布（判断 CAP 提高后能否填满）。不动内核、不碰 study.db。"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd

from diag.r9_breadth_loop import BASE_LIQUIDITY, _ann, _pm, _seg, build_universe
from diag.r10_global_opt_loop import split_trial, R10Runner
from discovery.split import extended_split


def main():
    tp = json.load(open("logs/r10_global_opt/verify_best_params.json",
                        encoding="utf-8"))
    params, state = split_trial(tp)
    print(f"champion filters: {state['filters']}", flush=True)

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

    R = R10Runner(params, lake, udates, split, first_dates)
    filled = R.filled_of(state)          # champion 组合过滤后信号池（同 loop 口径）
    inner = split.inner
    inners = [r for r in filled if inner.covers(pd.Timestamp(r["signal_date"]))]
    print(f"信号池: 全期 {len(filled)} 笔 / inner {len(inners)} 笔", flush=True)

    # ── 供给端：信号池并发分布（理论占用上限，CAP 能否填满的判据）──
    grid = udates[(udates >= pd.Timestamp(inner.start))
                  & (udates <= pd.Timestamp(inner.end))]
    grid_np = grid.to_numpy()
    starts = pd.DatetimeIndex([pd.Timestamp(r["buy_date"]) for r in inners])
    ends = pd.DatetimeIndex([pd.Timestamp(r["exit_date"]) for r in inners])
    s_idx = grid.searchsorted(starts)
    e_idx = grid.searchsorted(ends)
    conc = np.zeros(len(grid_np), dtype=int)
    for s, e in zip(s_idx, e_idx):
        conc[s:e] += 1
    print(f"\n供给端并发（inner 信号池理论占用）: mean={conc.mean():.1f} | "
          f"p50={np.percentile(conc,50):.0f} | p95={np.percentile(conc,95):.0f} | "
          f"max={conc.max()}", flush=True)
    for n in (4, 8, 13):
        print(f"  ≥{n} 并发占比（{n}×7.5%={n*7.5:.0f}% 占用档可填满的天数比例）: "
              f"{(conc >= n).mean()*100:.1f}%", flush=True)

    # ── 需求端：三档 max_positions 的 deployable 读数 ──
    print(f"\n{'档':>4} {'占用':>6} {'inner ann 中位':>14} {'n_taken':>8} "
          f"{'max_dd 中位':>10} {'ann/maxdd':>9}", flush=True)
    for n in (4, 8, 13):
        anns, taks, dds = [], [], []
        for s in range(5):
            m = _ann(inners, inner, udates, _pm(seed=s, max_positions=n))
            anns.append(m["ann"]); taks.append(m["n_taken"]); dds.append(m["max_dd"])
        a, t, d = float(np.median(anns)), float(np.median(taks)), float(np.median(dds))
        print(f"{n:>4} {n*7.5:>5.0f}% {a:>+14.4f} {t:>8.0f} {d:>10.4f} "
              f"{a/d if d>1e-9 else float('inf'):>9.2f}", flush=True)


if __name__ == "__main__":
    main()
