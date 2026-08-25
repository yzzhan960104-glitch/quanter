# -*- coding: utf-8 -*-
"""今日交易计划预览（离线，零状态库写入 · 2026-08-25）。

复刻 _eod 识别链（lake → load_universe → df_upto → 完整性 gate → build_strategy
(ACTIVE) → scan_live → build_orders_from_signals），与引擎逐环同源；唯一差异：
不写 state_store / 不推钉钉 / 不触发 pre_open——纯预览。

时序口径：data_day = 湖内最新交易日（今日盘中跑则为昨收），plan_date =
next_trading_day(data_day)。即本脚本回答「以当前 ACTIVE 参数，基于最近已收盘
数据，下一交易日的计划是什么」。

用法：
    PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -u diag/today_plan_preview.py
"""
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd


def main():
    from trading.data_ctx import load_df_upto, load_integrity_ctx, load_universe
    from trading import calendar
    from trading.critical import _trade_cfg
    from data.integrity import filter_universe_by_continuity
    from experiment.resolver import resolve_active
    from strategies.registry import build_strategy

    exps = resolve_active()
    assert exps, "无 ACTIVE 实验"
    exp = exps[0]
    print(f"[ACTIVE] {exp.experiment_id} weight={exp.weight}", flush=True)

    lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
    data_day = lake.index.get_level_values("date").max().date()
    plan_date = calendar.next_trading_day(data_day.isoformat())   # 该函数收 ISO 字符串
    print(f"[data] data_day={data_day}（湖内最新收盘）→ plan_date={plan_date}", flush=True)

    universe = load_universe(lake)
    df_map = {}
    for sym in universe:
        df_upto = load_df_upto(lake, sym, data_day)
        if df_upto is not None and len(df_upto) >= 60:
            df_map[sym] = df_upto
    print(f"[universe] {len(universe)} 只 → df_map {len(df_map)} 只（≥60 根）", flush=True)

    strategy = build_strategy(exp.strategy_name, cfg_override=exp.params)
    window = strategy.id_cfg.get("window", 60)
    susp, trade_days = load_integrity_ctx(data_day.isoformat())   # 同收 ISO 字符串
    clean = filter_universe_by_continuity(list(df_map.keys()), df_map, window, susp, trade_days)
    print(f"[integrity] 完整性 gate 后 {len(clean)} 只", flush=True)

    signals, atr_map = [], {}
    for sym in clean:
        try:
            for s in strategy.scan_live(sym, df_map[sym], data_day):
                s = replace(s, experiment_id=exp.experiment_id, experiment_weight=exp.weight)
                signals.append(s)
                if s.atr:
                    atr_map[sym] = s.atr
        except Exception as e:
            print(f"  [scan_live {sym} 异常跳过] {type(e).__name__}: {e}", flush=True)
    print(f"[signals] {len(signals)} 条（cooldown=0 不去重；momentum_gate=None 不过滤）",
          flush=True)

    from trading.compute.plan import build_orders_from_signals
    capital = float(os.getenv("TRADE_CAPITAL", "1_000_000"))
    pos_cap = float(os.getenv("TRADE_POS_CAP", "0.05"))
    orders = build_orders_from_signals(
        signals, capital=capital, pos_cap=pos_cap, atr_map=atr_map,
        stop_cfg=dict(_trade_cfg()))
    print(f"[orders] {len(orders)} 笔（budget/单 = {capital:.0f}×{pos_cap}×weight"
          f"={capital*pos_cap:.0f}）\n", flush=True)

    rows = []
    for o in orders:
        d = asdict(o) if hasattr(o, "__dataclass_fields__") else dict(o)
        rows.append(d)
        keys = ("symbol", "action", "price", "qty", "stop_price", "tp1", "tp2",
                "formed_at", "max_wait")
        print("  " + " | ".join(f"{k}={d.get(k)}" for k in keys if k in d), flush=True)
        meta = d.get("meta") or {}
        if meta:
            for mk in ("neckline", "atr", "cancel_on", "entry_rationale"):
                if mk in meta:
                    print(f"      {mk}={meta[mk]}", flush=True)

    out = {"active": exp.experiment_id, "data_day": str(data_day),
           "plan_date": str(plan_date), "n_signals": len(signals),
           "n_orders": len(orders), "orders": rows}
    path = "logs/today_plan_preview.json"
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False,
              indent=1, default=str)
    print(f"\n[done] {path}（预览产物，不入引擎状态库；正式计划由 18:00 EOD 产出）",
          flush=True)


if __name__ == "__main__":
    main()
