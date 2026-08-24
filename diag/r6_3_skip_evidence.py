# -*- coding: utf-8 -*-
"""R6-3 L3 追涨腿地基②：盲区月的 skip 流水证据（2026-08-26）。

beta 捕获率已确认 V 反月（2026-08 捕获 0.31）与两段弱急涨月（2025-06 0.16 /
2026-04 0.19）盲区。本脚本回答机制问题：**盲区月是「识别有信号、回踩等不到」
（skip_no_pullback/skip_target_met 主导——追涨腿对症）还是「识别本身无信号」
（skip 少——问题在识别层，追涨腿不对症）**。

方法：diag 内 monkeypatch bk.simulate_exit 收集全部返回（含 skip 字典——
scan_symbol 正常只计 n_skip 丢弃明细），一次 base 扫描后按月统计 filled /
skip_no_pullback / skip_target_met。零生产改动。

产物：logs/r6_3_skip_evidence.json。
"""
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd


def main():
    from discovery.snapshot import freeze
    from strategies.neckline import backtest as bk
    from discovery.objective import run_full_scan

    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    universe, meta = freeze("2021-01-01")

    sims = []
    orig = bk.simulate_exit

    def collect(sym_df, signal_idx, c_star, bottom, atr_val, exec=None, id_cfg=None):
        r = orig(sym_df, signal_idx, c_star, bottom, atr_val, exec=exec, id_cfg=id_cfg)
        if r is not None:
            sims.append(r)
        return r

    bk.simulate_exit = collect
    try:
        t0 = time.time()
        filled = run_full_scan(base, universe)
        print(f"[scan] n={len(filled)} collected_sims={len(sims)} "
              f"in {time.time()-t0:.0f}s", flush=True)
    finally:
        bk.simulate_exit = orig

    # 汇总口径：sims 里 filled 的 exit_reason 非 skip*；run_full_scan 的 momentum
    # 过滤（base 无闸）与 scan_symbol 的 n_skip 计数均以同一批 sims 为源。
    by_month = defaultdict(lambda: defaultdict(int))
    for r in sims:
        d = r.get("signal_date")
        if d is None:
            continue
        m = "%04d-%02d" % (d.year, d.month)
        by_month[m][r["exit_reason"]] += 1
        by_month[m]["total"] += 1

    months = sorted(by_month)
    print(f"\n{'month':>8} {'total':>6} {'filled':>7} {'no_pb':>6} {'tgt_met':>8} {'skip%':>6}",
          flush=True)
    for m in months:
        b = by_month[m]
        skip = b.get("skip_no_pullback", 0) + b.get("skip_target_met", 0)
        print("%8s %6d %7d %6d %8d %5.0f%%" % (
            m, b["total"], b["total"] - skip, b.get("skip_no_pullback", 0),
            b.get("skip_target_met", 0), 100 * skip / b["total"] if b["total"] else 0),
            flush=True)

    focus = ["2026-06", "2026-07", "2026-08", "2025-06", "2026-04"]
    out = {"by_month": {m: dict(by_month[m]) for m in months}, "focus": focus,
           "n_sims": len(sims)}
    json.dump(out, open("logs/r6_3_skip_evidence.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_3_skip_evidence.json", flush=True)


if __name__ == "__main__":
    main()
