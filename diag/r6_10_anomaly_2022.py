# -*- coding: utf-8 -*-
"""R6-10 · ③ 2022 反常归因（R5b 红旗① · 纯分析 · 2026-08-25）。

疑点：tp_h 收紧系（0.65-0.8）T+1 修复后 2022 折 +95.5%，所有历史参数集
2022 均 −9%~−25%——「熊市年大赚」形态翻转无机制解释。

假设 H：熊市超跌反弹的近端止盈高频兑现——2022 逐月阴跌中夹杂急反弹，
近端 tp（0.7H）在反弹日高频兑现小盈利，深回踩入场（bl 2.5）+ 近端止盈
在震荡下行年构成「买跌卖弹」网格；而宽 tp（1.5H）在反弹高度不足时
持仓漂移至止损。

方法：贪心栈 base vs base+tp_h=0.7 两组全历史 scan（同 universe），取
2022 年逐笔对照四维：月度 pnl 分布 / exit_reason 构成 / 持有天数 /
月度组合收益序列。产物 logs/r6_10_anomaly_2022.json。
"""
import json
import os
import sys
import time
from collections import defaultdict
from copy import deepcopy
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

MONTHS_2022 = [f"2022-{m:02d}" for m in range(1, 13)]


def main():
    from discovery.snapshot import freeze
    from discovery.objective import run_full_scan, portfolio_metrics
    from discovery.split import Segment

    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    universe, meta = freeze("2021-01-01")
    universe_dates = next(iter(universe.values())).index

    arms = [
        ("greedy(tp_h=1.5)", deepcopy(base)),
        ("tight(tp_h=0.7)", {**deepcopy(base), "tp_h_mult": 0.7}),
    ]
    out = {}
    for tag, p in arms:
        t0 = time.time()
        filled = run_full_scan(p, universe)
        y22 = [r for r in filled if str(r.get("signal_date"))[:4] == "2022"]
        # 月度逐笔分布
        by_month = defaultdict(list)
        for r in y22:
            by_month[str(r["signal_date"])[:7]].append(r)
        months = {}
        for m in sorted(by_month):
            rs = by_month[m]
            pnls = [r["avg_pnl_pct"] for r in rs]
            reasons = defaultdict(int)
            for r in rs:
                reasons[r["exit_reason"]] += 1
            months[m] = {
                "n": len(rs), "pnl_sum": round(sum(pnls), 1),
                "avg": round(sum(pnls) / len(pnls), 2),
                "win_rate": round(sum(1 for x in pnls if x > 0) / len(pnls), 3),
                "reasons": dict(reasons),
                "avg_holding": round(
                    sum(r.get("holding_bars") or 0 for r in rs) / len(rs), 1),
            }
        # 月度组合收益（组合口径 equity_end−1）
        mcal = {}
        for m in MONTHS_2022:
            y, mm = m.split("-")
            from calendar import monthrange
            seg = Segment(m, date(int(y), int(mm), 1),
                          date(int(y), int(mm), monthrange(int(y), int(mm))[1]))
            pm = portfolio_metrics(y22, seg, universe_dates)
            mcal[m] = round(pm["equity_end"] - 1.0, 4)
        # 2022 全年整体
        seg_y = Segment("y2022", date(2022, 1, 1), date(2022, 12, 31))
        yr = portfolio_metrics(y22, seg_y, universe_dates)
        out[tag] = {"n_2022": len(y22), "months": months, "monthly_equity": mcal,
                    "year_raw_ann": round(yr["ann"], 3),
                    "sec": round(time.time() - t0, 1)}
        print(f"[{tag}] 2022 n={len(y22)} ann={yr['ann']:+.1%} "
              f"({time.time()-t0:.0f}s)", flush=True)
        for m in MONTHS_2022:
            mm = months.get(m)
            me = mcal.get(m)
            if mm or (me or 0) != 0:
                print(f"    {m}: n={mm['n'] if mm else 0:>4} pnl_sum="
                      f"{mm['pnl_sum'] if mm else 0:>+8.1f} "
                      f"equity={me:+.1%} reasons={mm['reasons'] if mm else {}}",
                      flush=True)

    json.dump(out, open("logs/r6_10_anomaly_2022.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_10_anomaly_2022.json", flush=True)


if __name__ == "__main__":
    main()
