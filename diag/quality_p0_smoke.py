# -*- coding: utf-8 -*-
"""P0 特征库小样冒烟：5 符号全链路（扫描+事件+富化），检查行数/列/NaN 结构。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd


def main():
    from diag.quality_p0_features import STATE_PATH, _scan_pool
    from discovery.snapshot import freeze

    base = json.load(open(STATE_PATH, encoding="utf-8"))["base"]
    universe, meta = freeze("2021-01-01")
    small = dict(list(universe.items())[:5])
    df, n_fail = _scan_pool("smoke", base, small)
    print("\n列:", sorted(df.columns))
    print("\nNaN 占比 >0 的列:")
    nn = df.isna().mean()
    print(nn[nn > 0].round(3).to_string())
    print("\n样例行（关键列）:")
    cols = ["symbol", "signal_date", "avg_pnl_pct", "exit_reason", "h_atr",
            "touches", "bottom_disp", "suppression", "pattern_days", "ret20",
            "atr_pct", "bvr", "pv_corr10", "wait_days", "is_chase",
            "same_day_n", "pool_mom20", "pool_vol20"]
    print(df[cols].head(8).to_string())
    print(f"\nrows={len(df)} fail={n_fail}")


if __name__ == "__main__":
    main()
