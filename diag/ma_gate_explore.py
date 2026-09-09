# -*- coding: utf-8 -*-
"""MA(20/50/120/200)×颈线信号 逐笔打标探索(新分支 · 2026-09-05)。

口径纪律(09-04 幸存者偏差红线):
  - 逐笔期望差是必要非充分;本脚本只做方向探索,结论必须过 outer 窗
    + 组合口径 replay 才能当优化建议。
  - 被弃组必须同时呈报(查被弃笔是否仍盈利/笔数坍缩)。

产物:diag/ma_gate_trades_tagged.parquet(inner+outer 双窗逐笔含 MA 标签)。
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd

from backtest.replay import replay
from discovery.snapshot import freeze
from discovery.split import holdout_split
from experiment.resolver import resolve_champion
from strategies.neckline.strategy import NecklineMethodStrategy

MAS = (20, 50, 120, 200)

# ── ① 双窗逐笔(B3 冠军,与 r68_vs_b3_holdout 同源同池) ─────────────────
params = dict(resolve_champion().params)
universe, _ = freeze("2025-01-01")
split = holdout_split()
rows = []
for name in ("inner", "outer"):
    r = split.inner if name == "inner" else split.outer
    rep = replay(universe, NecklineMethodStrategy(cfg_override=params),
                 str(r.start), str(r.end))
    for t in rep.trades:
        rows.append({"seg": name, **{k: t[k] for k in (
            "symbol", "formed_at", "entry_date", "entry_price", "exit_date",
            "exit_price", "exit_reason", "rr", "holding_bars", "avg_pnl_pct",
            "neckline", "bottom", "atr")}})
    print(f"[seg {name}] trades={len(rep.trades)}", flush=True)
tr = pd.DataFrame(rows)
tr["formed_at"] = pd.to_datetime(tr["formed_at"])

# ── ② 数据湖 MA 打标(只加载有交易的标的;2023-09 起保证 200 日暖机) ────
syms = tr["symbol"].unique()
print(f"symbols with trades: {len(syms)}", flush=True)
lake = pd.read_parquet(
    "data_lake/a_shares_daily.parquet",
    filters=[("date", ">=", pd.Timestamp("2023-09-01"))],
    columns=["close"])
lake = lake[lake.index.get_level_values("symbol").isin(syms)]

tags = []
for sym, g in lake.groupby(level="symbol", sort=False):
    g = g.droplevel("symbol").sort_index()
    c = g["close"]
    d = {"symbol": sym, "date": g.index, "close": c.values}
    for m in MAS:
        ma = c.rolling(m, min_periods=m).mean()
        d[f"ma{m}"] = ma.values
        d[f"ma{m}_up"] = (ma > ma.shift(10)).values      # 10 日斜率向上
    tags.append(pd.DataFrame(d).dropna(subset=[f"ma{m}" for m in MAS]))
mg = pd.concat(tags)
mg["date"] = pd.to_datetime(mg["date"])

# ── ③ 合并打标(formed_at 当日快照 = 决策时点可用信息,无前视) ──────────
out = tr.merge(mg, left_on=["symbol", "formed_at"], right_on=["symbol", "date"],
               how="left")
miss = out["ma200"].isna().sum()
print(f"merged={len(out)}  ma缺失={miss} ({miss/len(out)*100:.1f}%)", flush=True)

for m in MAS:
    out[f"above{m}"] = out["close"] > out[f"ma{m}"]
    out[f"slope{m}"] = out[f"ma{m}_up"]
out["align_all"] = (out["above20"] & out["above50"] & out["above120"]
                    & out["above200"])
out["align_s50"] = out["above50"] & out["above200"]
out["dist200"] = out["close"] / out["ma200"] - 1.0
out["neck_above200"] = out["neckline"] > out["ma200"]

out.to_parquet("diag/ma_gate_trades_tagged.parquet")
print("saved diag/ma_gate_trades_tagged.parquet", flush=True)
print(out.groupby("seg")["avg_pnl_pct"].describe(), flush=True)
