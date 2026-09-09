# -*- coding: utf-8 -*-
"""复权口径一致性排查（P0-4a 修复后的存量体检 · 2026-09-09）。

背景：tushare_sync._sync_by_symbol 的 adj 完整性闸 2026-09-09 才补（此前 adj 拉空/
未覆盖时静默落未复权行，且 df_dt.map 缺日会把价格写成 NaN；增量 merge keep="last"
再顶掉旧 qfq 行）。本脚本对存量湖做体检，回答「缺陷历史上是否实际开过火」：

  ① NaN 体检：daily/weekly/monthly 三湖 OHLC 列 NaN 计数（NaN 写入路径的直接证据）；
  ② 错基体检（仅 daily）：close 日环比 |>35%| 且该标的此前已有 ≥10 根 K 线
     （排除上市初期无涨跌幅限制段）。qfq 湖内单日 35%+ 跳变在 20cm 板幅约束下
     不可达，命中即「未复权行混入」或「基线错定」的线索；
  ③ 双湖末端对齐抽查：weekly/monthly vs daily 同 symbol 末端同日 close 比值。
     同基线应 ≈1.0；比值显著偏离 = 两湖基线不同步（周/月线 shard 陈旧基线）
     的重定基排查线索。

输出 diag/adj_audit_<date>.json。注意：零检出 ≠ 零污染——轻度错基（除权幅度
<35%）与周/月线内部混入无法离线检出（需 raw 对照），本脚本只做「有无实弹损伤」
级排查；结论引用请附 json 路径（内联脚本结论可溯源纪律）。

用法：python diag/audit_adj_consistency.py [--jump 0.35] [--out diag/adj_audit.json]
"""
import argparse
import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

LAKE_DAILY = "data_lake/a_shares_daily.parquet"
LAKE_WEEKLY = "data_lake/a_shares_weekly.parquet"
LAKE_MONTHLY = "data_lake/a_shares_monthly.parquet"
OHLC = ["open", "high", "low", "close"]


def _nan_census(name, path, out):
    if not os.path.exists(path):
        out[name] = {"exists": False}
        return
    df = pd.read_parquet(path, columns=[c for c in OHLC])
    nan_counts = {c: int(df[c].isna().sum()) for c in df.columns}
    out[name] = {"exists": True, "rows": int(len(df)),
                 "symbols": int(df.index.get_level_values("symbol").nunique()),
                 "nan": nan_counts}


def _jump_census(out, jump_thr=0.35, min_bars=10):
    """仅 daily：|日环比| > jump_thr 且此前 ≥min_bars 根 K 线（排除新股无限制段）。"""
    if not os.path.exists(LAKE_DAILY):
        out["daily_jump"] = {"exists": False}
        return
    df = pd.read_parquet(LAKE_DAILY, columns=["close"])
    g = df.groupby(level="symbol")["close"]
    pct = g.pct_change()
    n_before = g.cumcount()
    mask = (pct.abs() > jump_thr) & (n_before >= min_bars) & pct.notna()
    hits = df[mask].copy()
    hits["pct"] = pct[mask]
    common = df.index.get_level_values("symbol").value_counts()
    common = common[common >= min_bars + 1]
    suspects = [
        {"symbol": s, "date": str(pd.Timestamp(d).date()), "pct": round(float(p), 4)}
        for (d, s), p in hits["pct"].sort_values(key=abs, ascending=False).items()
    ][:50]
    out["daily_jump"] = {
        "threshold": jump_thr, "min_bars": min_bars,
        "universe_symbols": int(len(common)),
        "n_hits": int(mask.sum()), "top_suspects": suspects,
    }


def _tail_alignment(out, main_name, main_path):
    """周/月线末端 close vs daily【同 symbol 同日】close 的比值分布（同基线应 ≈1）。

    注意比较对象：周/月线末根 K 的 trade_date=周末日/月末日在 daily 里本就有当日
    行——比值按 (date, symbol) 精确对齐取，而非两湖各自的末端日期（周期端点 vs
    最新交易日天然不同日，v1 误比对象致全量 mismatch 的教训）。
    """
    if not (os.path.exists(main_path) and os.path.exists(LAKE_DAILY)):
        out[f"{main_name}_tail_alignment"] = {"exists": False}
        return
    main = pd.read_parquet(main_path, columns=["close"])
    daily = pd.read_parquet(LAKE_DAILY, columns=["close"])
    # 各周/月 symbol 末根 (date, close)。注意：groupby 迭代给的是原始索引子集
    # （分组层不丢，ser.index 仍是 (date, symbol) 二层）——必须先 droplevel。
    pairs = {}
    for s, ser in main.groupby(level="symbol")["close"]:
        ser = ser.droplevel("symbol")
        d_last = ser.index.max()
        pairs[s] = (d_last, float(ser.loc[d_last]))
    # daily 同 (date, symbol) 精确对齐（merge 取齐——reindex 自建 MultiIndex 有
    # dtype 推断坑，v2 全量 NaN 的教训）
    q = pd.DataFrame([(d, s, v) for s, (d, v) in pairs.items()],
                     columns=["date", "symbol", "v_main"])
    q["date"] = pd.to_datetime(q["date"])   # DataFrame 构造出的 object 列 → datetime64
    ref = daily.reset_index()
    m = q.merge(ref, on=["date", "symbol"], how="left")
    ratios, missing = [], 0
    for _, row in m.iterrows():
        if pd.isna(row["close"]):
            missing += 1
            continue
        if float(row["close"]) > 0:
            ratios.append(float(row["v_main"]) / float(row["close"]))
    ratios = np.array(ratios) if ratios else np.array([])
    out[f"{main_name}_tail_alignment"] = {
        "checked": len(pairs),
        "same_date_pairs": int(len(ratios)),
        "daily_missing_that_date": missing,
        "ratio": {
            "min": round(float(ratios.min()), 4) if len(ratios) else None,
            "p05": round(float(np.quantile(ratios, 0.05)), 4) if len(ratios) else None,
            "median": round(float(np.median(ratios)), 4) if len(ratios) else None,
            "p95": round(float(np.quantile(ratios, 0.95)), 4) if len(ratios) else None,
            "max": round(float(ratios.max()), 4) if len(ratios) else None,
        } if len(ratios) else None,
        "n_ratio_outside_0p98_1p02": int(((ratios < 0.98) | (ratios > 1.02)).sum()),
        "worst_symbols": [
            {"symbol": str(r["symbol"]), "date": str(pd.Timestamp(r["date"]).date()),
             "ratio": round(float(r["v_main"]) / float(r["close"]), 4)}
            for _, r in m.dropna(subset=["close"]).assign(
                _dev=lambda x: (x["v_main"] / x["close"] - 1.0).abs()
            ).sort_values("_dev", ascending=False).head(15).iterrows()
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jump", type=float, default=0.35)
    ap.add_argument("--out", type=str,
                    default=f"diag/adj_audit_{datetime.now():%Y%m%d}.json")
    args = ap.parse_args()

    out = {"generated": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
           "jump_threshold": args.jump}
    _nan_census("daily", LAKE_DAILY, out)
    _nan_census("weekly", LAKE_WEEKLY, out)
    _nan_census("monthly", LAKE_MONTHLY, out)
    _jump_census(out, jump_thr=args.jump)
    _tail_alignment(out, "weekly", LAKE_WEEKLY)
    _tail_alignment(out, "monthly", LAKE_MONTHLY)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n[audit] 落盘 {args.out}")


if __name__ == "__main__":
    main()
