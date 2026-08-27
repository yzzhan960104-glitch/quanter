# -*- coding: utf-8 -*-
"""R6-5 腿 A/B 受控验证（2026-08-26 用户裁决「都测试一下」）。

R6-3 地基实锤的两盲区 + 对症腿（修复后引擎上验证）：
  腿 A（chase_entry）：垂直拉升月入场盲区——2024-10 池子 +9.7% 而 84% 信号
    skip_no_pullback 弃单；开=等待期无回踩改次日开盘市价追入。
  腿 B（timeout_extend_days）：V 反月出场盲区——2026-08 58% timeout 仅 10% tp2
    （tp 锚远+持有截断，R4 H0 线索）；开=超时浮盈≥5% 一次性延长持有。

预登记裁决规则（先于看数，防过拟合；单位 pp=百分点）：
  腿 A 过：2024-10 月收益 ≥ base+3pp ∧ 2024-09 ≥ base−2pp ∧ outer_raw ≥ base−1pp
    ∧ outer_mr ≥ base−1pp ∧ 无年段(2022-26)恶化>2pp。
  腿 B 过：2026-08 月收益 ≥ base+2pp ∧ 2026-08 timeout 占比下降 ∧ outer_raw ≥
    base−1pp ∧ outer_mr ≥ base−1pp ∧ 无年段恶化>2pp。
  组合臂（A+B20）仅参考——两条腿各自过闸后再谈叠加。

产物：logs/r6_5_legs_eval.json。
"""
import json
import os
import sys
import time
from copy import deepcopy
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

FOCUS_MONTHS = [("2024-09", 2024, 9), ("2024-10", 2024, 10),
                ("2026-07", 2026, 7), ("2026-08", 2026, 8), ("2025-08", 2025, 8)]
YEARS = [2022, 2023, 2024, 2025, 2026]


def main():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split, Segment
    from discovery.objective import run_full_scan, portfolio_metrics, evaluate_portfolio
    from discovery.manual_risk_sim import build_block_calendar

    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    t0 = time.time()
    universe, meta = freeze("2021-01-01")
    split = holdout_split()
    cal = build_block_calendar(universe)
    universe_dates = next(iter(universe.values())).index
    print(f"[init] freeze {len(universe)} syms in {time.time()-t0:.0f}s", flush=True)

    arms = [
        ("BASE", {}),
        ("A(chase)", {"chase_entry": True}),
        ("B10", {"timeout_extend_days": 10}),
        ("B20", {"timeout_extend_days": 20}),
        ("B30", {"timeout_extend_days": 30}),
        ("A+B20", {"chase_entry": True, "timeout_extend_days": 20}),
    ]
    month_segs = {tag: Segment(tag, date(y, m, 1), _month_end(y, m))
                  for tag, y, m in FOCUS_MONTHS}
    year_segs = {y: Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31)) for y in YEARS}

    results = {}
    for tag, over in arms:
        p = {**deepcopy(base), **over}
        t1 = time.time()
        filled = run_full_scan(p, universe)
        ev = evaluate_portfolio(p, universe, split, block_dates=cal)
        row = {"n_total": len(filled), "sec": round(time.time() - t1, 1),
               "outer_raw": ev["outer_raw"]["ann"] if "outer_raw" in ev else None,
               "outer_mr": ev["outer"]["ann"], "inner_raw": None, "inner_mr": ev["inner"]["ann"]}
        # inner_raw：block_dates 口径下 evaluate 附 inner_raw；无则同 inner（base 无线时）
        row["inner_raw"] = ev.get("inner_raw", ev["inner"])["ann"]
        row["months"] = {}
        for tag_m, seg in month_segs.items():
            m = portfolio_metrics(filled, seg, universe_dates)
            row["months"][tag_m] = {"ret": m["equity_end"] - 1.0, "n": m["n"]}
        row["years"] = {y: portfolio_metrics(filled, seg, universe_dates)["ann"]
                        for y, seg in year_segs.items()}
        # 2026-08 exit 结构：timeout 占比（腿 B 机制焦点）
        aug = [r for r in filled
               if pd.Timestamp(r["signal_date"]).year == 2026
               and pd.Timestamp(r["signal_date"]).month == 8]
        row["aug26_timeout_share"] = (sum(1 for r in aug if r["exit_reason"] == "timeout")
                                      / len(aug)) if aug else None
        row["aug26_tp2_share"] = (sum(1 for r in aug if r["exit_reason"] == "tp2")
                                  / len(aug)) if aug else None
        results[tag] = row
        mtxt = " ".join(f"{k}:{v['ret']:+.1%}" for k, v in row["months"].items())
        print(f"[{tag:>7}] n={row['n_total']:>5} outer_raw {row['outer_raw']:+.1%} "
              f"mr {row['outer_mr']:+.1%} | {mtxt}", flush=True)

    b = results["BASE"]
    print("\n=== 年段 raw ann（base 对照）===", flush=True)
    for tag, row in results.items():
        ytxt = " ".join(f"y{y}:{row['years'][y]:+.0%}"
                        f"({(row['years'][y]-b['years'][y])*100:+.0f}pp)"
                        for y in YEARS)
        print(f"[{tag:>7}] {ytxt}", flush=True)

    def years_ok(row):
        return all(row["years"][y] >= b["years"][y] - 0.02 for y in YEARS)

    a = results["A(chase)"]
    a_pass = (a["months"]["2024-10"]["ret"] >= b["months"]["2024-10"]["ret"] + 0.03
              and a["months"]["2024-09"]["ret"] >= b["months"]["2024-09"]["ret"] - 0.02
              and a["outer_raw"] >= b["outer_raw"] - 0.01
              and a["outer_mr"] >= b["outer_mr"] - 0.01
              and years_ok(a))
    b20 = results["B20"]
    b_pass = (b20["months"]["2026-08"]["ret"] >= b["months"]["2026-08"]["ret"] + 0.02
              and (b20["aug26_timeout_share"] or 1) < (b["aug26_timeout_share"] or 1)
              and b20["outer_raw"] >= b["outer_raw"] - 0.01
              and b20["outer_mr"] >= b["outer_mr"] - 0.01
              and years_ok(b20))
    print(f"\n[verdict] 腿 A（垂直月追入）: {'过闸' if a_pass else '不过闸'}", flush=True)
    print(f"[verdict] 腿 B（超时延长 B20）: {'过闸' if b_pass else '不过闸'}", flush=True)
    print(f"[verdict] 2026-08 timeout 占比 base {b['aug26_timeout_share']:.0%} → "
          f"B20 {b20['aug26_timeout_share']:.0%}（tp2 {b['aug26_tp2_share']:.0%} → "
          f"{b20['aug26_tp2_share']:.0%}）", flush=True)

    json.dump({"arms": results, "verdict": {"a_pass": a_pass, "b_pass": b_pass}},
              open("logs/r6_5_legs_eval.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_5_legs_eval.json", flush=True)


def _month_end(y, m):
    from calendar import monthrange
    return date(y, m, monthrange(y, m)[1])


if __name__ == "__main__":
    main()
