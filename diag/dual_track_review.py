# -*- coding: utf-8 -*-
"""L4 双轨逐日对账器（QMT 引擎腿 · 2026-08-27 起，B3 ACTIVE 时代）。

读 logs/trading_state.db（order/fill/trade_event/account_daily），产出：
  1. 每日漏斗：SIGNAL → CONFIRMED → BLOCKED/ORDERED → REJECTED/FILLED；
  2. 成交假设审计（L4 核心）：每笔 OPEN 限价单 vs 湖数据「回测口径当日
     low≤限价=应成交」判定——不一致清单（回测说成交、实盘未成交=最大摩擦源，
     即调度保费发现后第一个要实测的执行面数字）；
  3. 权益对账（account_daily）与调度保费预测追踪（实盘=计划序，远低于 oracle
     +400% 量级即实锤——本表只记账不预判）。

纯读侧：不写 trading_state.db、不触引擎。产物 logs/dual_track/review_*.md+json。

用法：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/dual_track_review.py [--since 2026-08-26]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

DB = "logs/trading_state.db"
OUT_DIR = "logs/dual_track"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-26")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    import sqlite3
    con = sqlite3.connect(DB)

    ev = pd.read_sql("SELECT * FROM trade_event", con)
    ev["day"] = ev["timestamp"].str.slice(0, 10)
    orders = pd.read_sql('SELECT * FROM "order"', con)
    fills = pd.read_sql("SELECT * FROM fill", con)
    ad = pd.read_sql("SELECT * FROM account_daily ORDER BY date", con)

    # —— 1. 每日漏斗 ——
    funnel = (ev[ev["day"] >= args.since]
              .groupby(["day", "action"]).size().unstack(fill_value=0))
    od = (orders[orders["trade_date"] >= args.since]
          .groupby(["trade_date", "state", "purpose"]).size()
          .rename("n").reset_index())

    # —— 2. 成交假设审计（OPEN 限价单 vs 湖 low）——
    opens = orders[(orders["purpose"] == "OPEN")
                   & (orders["trade_date"] >= args.since)].copy()
    audit_rows, n_should_fill_unfilled = [], 0
    lk = None
    if len(opens):
        need_syms = opens["symbol"].unique().tolist()
        try:
            lk = pd.read_parquet("data_lake/a_shares_daily.parquet",
                                 filters=[("symbol", "in", need_syms)])
        except Exception as e:  # noqa: BLE001
            print(f"[audit][WARN] 湖读取失败：{e}", flush=True)
        if lk is not None:
            for _, r in opens.iterrows():
                d = pd.Timestamp(r["trade_date"])
                try:
                    sym_df = lk[lk.index.get_level_values("symbol") == r["symbol"]]
                    row_lows = sym_df["low"]
                    dates = sym_df.index.get_level_values("date")
                    low_d = None
                    if len(row_lows):
                        # 当日（挂单日）及其后两日回看：限价单当日或次日回踩
                        mask = (dates >= d) & (dates <= d + pd.Timedelta(days=3))
                        if mask.any():
                            low_d = float(row_lows[mask].min())
                except Exception:
                    low_d = None
                filled = r["state"] == "FILLED"
                should = (low_d is not None and low_d <= float(r["price"]))
                if should and not filled:
                    n_should_fill_unfilled += 1
                audit_rows.append({
                    "trade_date": r["trade_date"], "symbol": r["symbol"],
                    "limit": r["price"], "state": r["state"],
                    "low_3d": low_d, "bt_should_fill": should,
                    "mismatch": bool(should and not filled)})
    audit = pd.DataFrame(audit_rows)

    # —— 3. 权益对账 ——
    ad_recent = ad[ad["date"] >= args.since]

    L = []
    ap_ = L.append
    ap_(f"# 双轨对账（QMT 引擎腿 · since {args.since} · 生成于 "
        f"{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}）")
    ap_("")
    ap_("## 1. 每日事件漏斗")
    ap_("")
    ap_(funnel.to_string() if len(funnel) else "（无事件）")
    ap_("")
    ap_("## 2. 订单状态分布")
    ap_("")
    ap_(od.to_string(index=False) if len(od) else "（无订单）")
    ap_("")
    ap_("## 3. 成交假设审计（回测口径 low≤限价=应成交）")
    ap_("")
    if len(audit):
        ap_(f"- OPEN 单 {len(audit)} 笔：回测应成交但实盘未成交 "
            f"**{n_should_fill_unfilled} 笔**（"
            f"{n_should_fill_unfilled / len(audit):.0%}）——L4 执行摩擦主源")
        mm = audit[audit["mismatch"]]
        if len(mm):
            ap_("- 不一致明细（前 15）：")
            ap_(mm.head(15).to_string(index=False))
    else:
        ap_("-（窗口内无 OPEN 单）")
    ap_("")
    ap_("## 4. 权益快照")
    ap_("")
    ap_(ad_recent.to_string(index=False) if len(ad_recent) else "（无快照）")
    ap_("")
    ap_("## 5. 调度保费预测追踪（记账不预判）")
    ap_("")
    ap_("- 可证伪预测：实盘（计划序）收益应远低于 oracle 口径 +400% 量级，"
        "接近 deployable 中位（外层 +5.7%/全期 +4.9%）则调度保费机制实锤。")
    ap_("- 当前统计见上表；样本积累中。")
    ap_("")

    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M")
    md = os.path.join(OUT_DIR, f"review_{stamp}.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    js = {"since": args.since, "n_orders": int(len(orders)),
          "n_opens": int(len(audit)),
          "n_should_fill_unfilled": int(n_should_fill_unfilled),
          "state_counts": orders[orders["trade_date"] >= args.since]
          .groupby("state").size().to_dict(),
          "equity_last": ad_recent.tail(1).to_dict("records"),
          "generated_at": pd.Timestamp.now().isoformat()}
    with open(md.replace(".md", ".json"), "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=1, default=str)
    print(f"[done] {md}")
    print(f"[summary] OPEN {js['n_opens']} 笔 / 应成交未成交 "
          f"{js['n_should_fill_unfilled']} / 状态 {js['state_counts']}")


if __name__ == "__main__":
    main()
