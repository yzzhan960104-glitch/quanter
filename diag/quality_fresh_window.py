# -*- coding: utf-8 -*-
"""新窗口统一重证发射器（H-R7a 均值映射特征 + H-R7d occ 特征 · 2026-08-27 预登记）。

════════════════════════════════════════════════════════════════════
预登记协议（2026-08-27 写死——此刻新窗口（cutoff 2026-08-27 起）为空，
这是最纯的预登记时机：数据尚未发生，闸先于任何新窗口读数存在）
════════════════════════════════════════════════════════════════════
背景：H-R7a（bottom_disp/vol5_slope → pnl 判别力，部署序下组合增益归零）与
H-R7d occ 型（suppression/ret20/ret60/atr_pct/vol5_slope 负向 → 短占用优先，
双段正但闸差一票）均列入「待新窗口确认」队列。本脚本=窗口成熟后一条命令出
裁决的发射器；判定单位=**新窗口信号口径**（组合级欠功效，只报告不裁决）。

窗口定义：主池（freeze 2021-01-01 今日截面）全宇宙重扫后取 signal_date ≥
CUTOFF（默认 2026-08-27）的成交笔（尾部未走完信号按回测同款边界处理）。
成熟条件：窗口信号 ≥15 个交易日（湖尾−cutoff ≥15 个 trading day）且成交
笔 ≥600；不足则输出「窗口不足」退出码 3（不产裁决）。

预登记检验（窗口成熟后执行，分位自窗口内计算）：
  R7a 检验：bottom_disp 与 vol5_slope 各自对 avg_pnl_pct 的 hi30−lo30 对比
    ≥ 0（方向=高好）；两特征同时 ≥0 → R7a 特征存活；一负 → 杀（登记否决）。
  R7d 检验：五特征（suppression/ret20/ret60/atr_pct/vol5_slope）各自对
    occupancy（wait_days+holding_bars）的 hi30−lo30 ≥ 0（高特征→长占用）；
    ≥4/5 同向 → 存活；≤2/5 → 杀；3/5 → 悬置续窗。
  样本门槛：每侧 n≥100 才计入判定；组合级（random 21 种子中位 vs
  priority/均值映射两臂的新窗口 ann）欠功效，只入报告。
  检验只做一次：裁决产出后本窗口作废，不复检（防窗内多次偷看）——
  state 记录已裁决的 cutoff，重复运行同 cutoff 直接拒绝。

用法：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_fresh_window.py [--cutoff 2026-08-27]
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

STATE_PATH = "logs/quality/fresh_window_state.json"
OUT_JSON = "logs/quality/fresh_window_verdict.json"
R7A_FEATS = ["bottom_disp", "vol5_slope"]
R7D_OCC_FEATS = ["suppression", "ret20", "ret60", "atr_pct", "vol5_slope"]
MIN_DAYS = 15
MIN_TRADES = 600
MIN_SIDE_N = 100


def _contrast(df, col, target):
    s = df[col].dropna()
    if len(s) < 300:
        return None, None
    lo_t, hi_t = s.quantile([0.3, 0.7])
    lo = df.loc[df[col] <= lo_t, target]
    hi = df.loc[df[col] >= hi_t, target]
    if len(lo) < MIN_SIDE_N or len(hi) < MIN_SIDE_N:
        return None, None
    return float(hi.mean() - lo.mean()), (len(lo), len(hi))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2026-08-27")
    args = ap.parse_args()
    cutoff = pd.Timestamp(args.cutoff)

    if os.path.exists(STATE_PATH):
        st0 = json.load(open(STATE_PATH, encoding="utf-8"))
        if st0.get("adjudicated_cutoff") == args.cutoff:
            print(f"[guard] cutoff={args.cutoff} 已裁决过（{st0.get('verdict')}）"
                  "——同一窗口不重复检验（防偷看），换新 cutoff 再来", flush=True)
            sys.exit(4)

    from diag.quality_p0_features import STATE_PATH as BASE_PATH, _scan_pool
    base = json.load(open(BASE_PATH, encoding="utf-8"))["base"]
    from discovery.snapshot import freeze
    print(f"[boot] freeze('2021-01-01') 重扫主池（识别缓存红利，全宇宙）...",
          flush=True)
    universe, meta = freeze("2021-01-01")
    df, _ = _scan_pool("fresh", base, universe)
    w = df[df["signal_date"] >= cutoff].copy()
    n_days = w["signal_date"].nunique()
    print(f"[window] cutoff={args.cutoff} 成交笔={len(w)} 信号日={n_days}",
          flush=True)
    if len(w) < MIN_TRADES or n_days < MIN_DAYS:
        print(f"[窗口不足] 需 ≥{MIN_TRADES} 笔 / ≥{MIN_DAYS} 信号日——不产裁决"
              f"（当前 {len(w)}/{n_days}），窗口成熟后重跑", flush=True)
        # 记录进度但不裁决
        json.dump({"cutoff": args.cutoff, "n": len(w), "days": n_days,
                   "checked_at": pd.Timestamp.now().isoformat(),
                   "adjudicated": False},
                  open(STATE_PATH, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        sys.exit(3)

    w["occ"] = w["wait_days"] + w["holding_bars"]
    out = {"cutoff": args.cutoff, "n": len(w), "days": n_days,
           "adjudicated": True,
           "checked_at": pd.Timestamp.now().isoformat()}

    # —— R7a 检验 ——
    r7a = {}
    for c in R7A_FEATS:
        d, ns = _contrast(w, c, "avg_pnl_pct")
        r7a[c] = {"contrast": None if d is None else round(d, 3), "n_sides": ns}
    r7a_ok = all(v["contrast"] is not None and v["contrast"] >= 0
                 for v in r7a.values())
    out["r7a"] = {"detail": r7a, "verdict": "ALIVE" if r7a_ok else "KILLED"}

    # —— R7d occ 检验 ——
    r7d = {}
    n_pos = 0
    judged = 0
    for c in R7D_OCC_FEATS:
        d, ns = _contrast(w, c, "occ")
        r7d[c] = {"contrast": None if d is None else round(d, 3), "n_sides": ns}
        if d is not None:
            judged += 1
            if d >= 0:
                n_pos += 1
    r7d_v = ("ALIVE" if n_pos >= 4 else
             "KILLED" if n_pos <= 2 and judged >= 4 else "PENDING")
    out["r7d_occ"] = {"detail": r7d, "n_pos": f"{n_pos}/{judged}",
                      "verdict": r7d_v}

    # —— 组合级（欠功效，只报告）——
    try:
        from diag.quality_p2_layering import _ann, _pm, _to_filled
        from diag.quality_r7c_priority import vector_score
        import pyarrow.parquet as pq
        _dates = pq.read_table("data_lake/a_shares_daily.parquet",
                               columns=["date"]).column("date").to_pandas()
        udates = pd.DatetimeIndex(sorted(_dates.unique()))
        from discovery.split import Segment
        d0 = cutoff.date()
        seg = Segment("fresh", d0, date(2026, 12, 31))
        udates_w = udates[udates >= cutoff]
        filled = _to_filled(w)
        rnd = [_ann(filled, seg, udates_w, _pm(queue_order="random",
                                               queue_seed=s))["ann"]
               for s in range(21)]
        train = df[(df["year"] >= 2022) & (df["year"] <= 2024)]
        occ_dirs = {c: -1 for c in R7D_OCC_FEATS}
        w2 = w.copy()
        w2["priority"] = vector_score(w2, occ_dirs, train)
        pri_map = dict(zip(zip(w2["symbol"], w2["signal_date"]), w2["priority"]))
        filled_pri = _to_filled(w2)
        for t in filled_pri:
            t["priority"] = pri_map[(t["symbol"], t["signal_date"])]
        pri = _ann(filled_pri, seg, udates_w, _pm(queue_order="priority"))
        out["portfolio_report_only"] = {
            "note": "窗口短、taken 少——欠功效，不构成裁决依据",
            "random_med": round(float(np.median(rnd)), 4),
            "priority_occ": {"ann": round(pri["ann"], 4),
                             "n_taken": pri["n_taken"]},
        }
    except Exception as e:  # noqa: BLE001
        out["portfolio_report_only"] = {"error": str(e)}

    out["verdict"] = {"r7a": out["r7a"]["verdict"], "r7d": out["r7d_occ"]["verdict"]}
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    json.dump({"adjudicated_cutoff": args.cutoff,
               "verdict": out["verdict"],
               "checked_at": out["checked_at"]},
              open(STATE_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps({"r7a": r7a, "r7a_verdict": out["r7a"]["verdict"],
                      "r7d": r7d, "r7d_verdict": r7d_v,
                      "portfolio": out.get("portfolio_report_only")},
                     ensure_ascii=False, indent=1), flush=True)
    print(f"[done] {OUT_JSON}", flush=True)


if __name__ == "__main__":
    main()
