# -*- coding: utf-8 -*-
"""R6-2 弱动量×深形态条件交互（H1 对症手术刀 · 2026-08-26）。

R4 实锤：止损三死法族同向复现（弱动量/深形态/贴颈线），但 H1 单维动量闸
受控否决——判别力≠可治疗性（阈值无差别切除同区正贡献信号，拦掉的 313 笔
赚的比亏的多）。本脚本验证二阶假设：**死法的判别力在【弱动量×深形态】的
联合条件上**——单独弱动量含幸存者（浅+弱可活）、单独深形态含幸存者（深+
强可活），AND 条件瞄准两缺陷叠加的真死区（R6 排程②）。

方法（逐笔范式纪律，昂贵步共享一次的 R6-1 精神）：
  1. 一次 base 扫描（贪心栈，R6a 真值口径 outer_raw +83.5%）→ 逐笔特征表：
     m20（突破日个股 20 日收益，run_full_scan momentum_gate 同款因果口径）
     × H_over_ATR（识别期形态深度，无前视）× pnl/exit_reason；
  2. 格网 (m_thr × d_thr) 三臂：AND 交互闸 / 纯动量闸 / 纯深形态闸——同扫描
     产物后过滤，build_equity_curve 单源算 inner(2025)/outer(2026) 双口径
     （raw + 模拟线 mr）+ 逐年 raw ann 反事实，与 no-gate base 对拍；
  3. 预登记裁决规则（防 20 格过拟合，机制先于读数）：
     ① 机制：blocked 止损率 ≥ kept 的 1.5× 且 blocked pnl_sum < 0；
     ② 收益：outer_raw ↑ 且 outer_mr（模拟线）不降；
     ③ 诚实性：2022 不恶化 + ≥3 年段（2022-2026 五年）改善/持平（±1pp 内
        视为持平）+ 无年段恶化超 2pp；
     ④ 对照：AND 臂在机制浓度（blocked 止损率）与截留正贡献（blocked pnl
        密度）上须优于同阈值单维臂——否则交互假设不成立，H1 同款否决。

产物：logs/r6_2_interaction.json（gitignored）。
"""
import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

M_THRS = [0.0, 0.02, 0.03, 0.05, 0.08]
D_THRS = [2.8, 3.2, 3.6, 4.0]   # base max_h_atr=4.5 无条件上限内取界
FLAT_PP = 1.0                    # 年段持平带宽（pp）
WORSE_PP = 2.0                   # 年段恶化红线（pp）


def main():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split, Segment
    from discovery.objective import run_full_scan, portfolio_metrics
    from discovery.manual_risk_sim import build_block_calendar

    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    t0 = time.time()
    universe, meta = freeze("2021-01-01")
    split = holdout_split()
    cal = build_block_calendar(universe)
    print(f"[init] freeze {len(universe)} syms snapshot={meta.snapshot_hash} "
          f"in {time.time()-t0:.0f}s", flush=True)

    t1 = time.time()
    filled = run_full_scan(base, universe)
    print(f"[scan] base n_trades={len(filled)} in {time.time()-t1:.0f}s", flush=True)

    # —— 逐笔特征表（m20 因果：截至 signal_date 的 20 日收益；h=识别期 H/ATR）——
    m20_map = {sym: df["close"] / df["close"].shift(20) - 1.0
               for sym, df in universe.items()}

    def m20_of(r):
        s = m20_map.get(r["symbol"])
        if s is None:
            return None
        ts = pd.Timestamp(r["signal_date"])
        if ts in s.index:
            v = s.loc[ts]
            return None if pd.isna(v) else float(v)
        return None   # 缺值中性放行（run_full_scan momentum_gate 同款容错）

    rows = []
    for r in filled:
        rows.append({"m20": m20_of(r), "h": r.get("H_over_ATR"),
                     "pnl": r["avg_pnl_pct"], "stop": r["exit_reason"] == "stop_loss",
                     "r": r})

    universe_dates = next(iter(universe.values())).index
    segs = {"inner": split.inner, "outer": split.outer}
    years = [2022, 2023, 2024, 2025, 2026]
    year_segs = {y: Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31)) for y in years}

    def evaluate(trades):
        out = {"n": len(trades)}
        for k, seg in segs.items():
            out[k + "_raw"] = portfolio_metrics(trades, seg, universe_dates)["ann"]
            out[k + "_mr"] = portfolio_metrics(
                trades, seg, universe_dates, block_dates=cal)["ann"]
        for y, seg in year_segs.items():
            out["y%d" % y] = portfolio_metrics(trades, seg, universe_dates)["ann"]
        return out

    base_eval = evaluate(filled)
    print("[base] outer_raw {:+.1%} outer_mr {:+.1%} inner_raw {:+.1%} | "
          "years: ".format(base_eval["outer_raw"], base_eval["outer_mr"],
                           base_eval["inner_raw"])
          + " ".join("y%d:%+.0f%%" % (y, base_eval["y%d" % y] * 100) for y in years),
          flush=True)

    n_all = len(rows)
    stop_rate_all = sum(w["stop"] for w in rows) / n_all if n_all else 0.0

    def arm(name, m_thr, d_thr, mode):
        if mode == "and":
            blocked = [w for w in rows
                       if w["m20"] is not None and w["m20"] < m_thr
                       and w["h"] is not None and w["h"] > d_thr]
        elif mode == "mom":
            blocked = [w for w in rows if w["m20"] is not None and w["m20"] < m_thr]
        else:
            blocked = [w for w in rows if w["h"] is not None and w["h"] > d_thr]
        blk_ids = {id(w) for w in blocked}
        kept_rows = [w for w in rows if id(w) not in blk_ids]
        kept = [w["r"] for w in kept_rows]
        nb = len(blocked)
        cell = {"arm": name, "m_thr": m_thr, "d_thr": d_thr, "mode": mode,
                "n_blocked": nb, "blocked_share": round(nb / n_all, 4)}
        if nb:
            cell["stop_rate_blocked"] = round(sum(w["stop"] for w in blocked) / nb, 4)
            cell["pnl_sum_blocked"] = round(sum(w["pnl"] for w in blocked), 1)
            cell["avg_pnl_blocked"] = round(sum(w["pnl"] for w in blocked) / nb, 2)
        nk = len(kept_rows)
        cell["stop_rate_kept"] = round(sum(w["stop"] for w in kept_rows) / nk, 4) if nk else None
        cell.update(evaluate(kept))
        return cell

    results = []
    for m_thr in M_THRS:
        for d_thr in D_THRS:
            results.append(arm("and(m<%s × h>%s)" % (m_thr, d_thr), m_thr, d_thr, "and"))
        results.append(arm("mom(m<%s)" % m_thr, m_thr, None, "mom"))
    for d_thr in D_THRS:
        results.append(arm("depth(h>%s)" % d_thr, None, d_thr, "depth"))

    # —— 机制与裁决标记（预登记规则）——
    verdicts = []
    for c in results:
        mech_ok = (c.get("stop_rate_blocked", 0) >= 1.5 * (c["stop_rate_kept"] or 0)
                   and c.get("pnl_sum_blocked", 0) < 0)
        yrs_improve = 0
        no_worse = True
        for y in years:
            k = "y%d" % y
            delta = (c[k] - base_eval[k]) * 100
            if delta > FLAT_PP:
                yrs_improve += 1
            if delta < -WORSE_PP:
                no_worse = False
        c["mech_ok"] = bool(mech_ok)
        c["yrs_improve"] = yrs_improve
        c["no_year_worse_2pp"] = no_worse
        c["outer_raw_up"] = c["outer_raw"] > base_eval["outer_raw"]
        c["outer_mr_not_down"] = c["outer_mr"] >= base_eval["outer_mr"]
        c["y2022_not_worse"] = c["y2022"] >= base_eval["y2022"] - FLAT_PP / 100
        c["adopt"] = bool(mech_ok and c["outer_raw_up"] and c["outer_mr_not_down"]
                          and yrs_improve >= 3 and no_worse and c["y2022_not_worse"])
        if c["mode"] == "and":
            verdicts.append(c)

    print("\n=== AND 交互闸格网（blocked 机制 × 反事实读数）===", flush=True)
    print("{:>26} {:>6} {:>7} {:>9} {:>10} {:>9} {:>10} {:>5} {}".format(
        "arm", "n_blk", "stop_b", "pnl_sum", "outer_raw", "outer_mr", "inner_raw",
        "yrs+", "adopt"), flush=True)
    for c in verdicts:
        print("{:>26} {:>6} {:>7.0%} {:>8.1f} {:>9.1%} {:>9.1%} {:>9.1%} {:>6} {}".format(
            c["arm"], c["n_blocked"], c.get("stop_rate_blocked", 0),
            c.get("pnl_sum_blocked", 0), c["outer_raw"], c["outer_mr"],
            c["inner_raw"], c["yrs_improve"], "ADOPT" if c["adopt"] else ""),
            flush=True)

    print("\n=== 单维对照臂（H1 复现：纯动量/纯深形态）===", flush=True)
    for c in results:
        if c["mode"] != "and":
            print("{:>26} {:>6} stop_b={:.0%} pnl_sum={:.1f} outer_raw={:+.1%}".format(
                c["arm"], c["n_blocked"], c.get("stop_rate_blocked", 0),
                c.get("pnl_sum_blocked", 0), c["outer_raw"]), flush=True)

    adopted = [c for c in verdicts if c["adopt"]]
    concl = ("无格过预登记规则——交互假设否决（H1 同款闭环）" if not adopted
             else "过闸候选 %d 个（见上表 ADOPT）" % len(adopted))
    print(f"\n[verdict] {concl}", flush=True)
    print(f"[verdict] base stop_rate={stop_rate_all:.1%}（kept 浓度对照基准）", flush=True)

    json.dump({"base": base_eval, "stop_rate_all": stop_rate_all,
               "grid": results, "n_adopted": len(adopted)},
              open("logs/r6_2_interaction.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_2_interaction.json", flush=True)


if __name__ == "__main__":
    main()
