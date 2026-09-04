# -*- coding: utf-8 -*-
"""动量衰竭×假突破 因子深扫（2026-09-04 · 1 小时时间盒）。

假设组（全部来自当日归因实证）：
  H1 动量衰竭：进场前 20 日涨幅透支（蓝思 +26.6%/昆仑万维两日 +13.7%）→
     momentum_gate（突破日 20 日收益 < gate 才入场；**当前 None=关闭**，
     R4-H1 维度从未启用）扫 0.15/0.20/0.25/0.30
  H2 假突破：突破日量能不足（归因 8/10 只 0.57×-1.03×）→
     breakout_vol_mult 扫 1.15/1.3/1.5（1.1-1.8 历史空白带）
  H3 组合：单因子最优组合复验

并行：ProcessPoolExecutor 3 并发跑 evaluate_replay（inner=2025 全市场，
冠军其余参数持住）；逐笔量比画像在主进程向量化（零回测成本——基线逐笔
×湖量能，按突破日量比与 20 日涨幅双维分桶）。
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

GRID = [("momentum_gate", v) for v in (0.15, 0.20, 0.25, 0.30)] + \
       [("breakout_vol_mult", v) for v in (1.15, 1.3, 1.5)]
WORKERS = 3


def _cell(job):
    """单格：冠军参数 + 一维覆盖 → inner 评估（子进程独立加载湖）。"""
    key, val = job
    from discovery.objective import evaluate_replay
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from experiment.resolver import resolve_champion
    base = dict(resolve_champion().params)
    split = holdout_split()
    universe, _ = freeze("2025-01-01")
    m = evaluate_replay({**base, key: val}, universe, split,
                        start=str(split.inner.start),
                        end=str(split.inner.end)).get("inner") or {}
    return {"key": key, "val": val,
            **{k: m.get(k) for k in ("n_hits", "win_rate", "avg_rr",
                                     "annualized_return", "max_drawdown")}}


def _profile():
    """逐笔画像：基线逐笔 ×（突破日量比 + 20 日涨幅）双维分桶。"""
    import numpy as np
    import pandas as pd
    from backtest.replay import replay
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from experiment.resolver import resolve_champion
    from strategies.neckline.strategy import NecklineMethodStrategy

    base = dict(resolve_champion().params)
    split = holdout_split()
    universe, _ = freeze("2025-01-01")
    strat = NecklineMethodStrategy(cfg_override=base)
    rep = replay(universe, strat, str(split.inner.start), str(split.inner.end))
    trades = [t for t in rep.trades
              if t.get("entry_price") and t.get("neckline") and t.get("atr")]

    df = pd.read_parquet(ROOT / "data_lake" / "a_shares_daily.parquet",
                         columns=["close", "volume"])
    by_sym = {s: g.sort_index() for s, g in df.groupby(level="symbol")}
    rows = []
    for t in trades:
        sym, bd = t.get("symbol"), t.get("breakout_date") or t.get("entry_date")
        g = by_sym.get(sym)
        if g is None or not bd:
            continue
        bd = str(bd)[:10]
        try:
            idx = g.index.get_level_values("date").get_loc(bd)
        except KeyError:
            continue
        if idx < 21:
            continue
        win = g.iloc[idx - 20: idx + 1]
        vol_ratio = (win["volume"].iloc[-1] / max(1e-9, win["volume"].iloc[:-1].mean())
                     if win["volume"].iloc[:-1].mean() > 0 else None)
        mom20 = win["close"].iloc[-1] / win["close"].iloc[0] - 1
        rows.append({"vol_ratio": round(float(vol_ratio), 2) if vol_ratio else None,
                     "mom20": round(float(mom20), 3),
                     "pnl": t.get("avg_pnl_pct"), "rr": t.get("rr"),
                     "prem": (t["entry_price"] - t["neckline"]) / t["atr"]})
    xs = [r for r in rows if r["pnl"] is not None and r["vol_ratio"] is not None]

    def _bucket(key, edges):
        out = []
        for i, (lo, hi) in enumerate(zip(edges, edges[1:])):
            seg = [r for r in xs if lo <= r[key] < hi]
            if not seg:
                continue
            pnls = [r["pnl"] for r in seg]
            out.append({"range": f"{lo}~{hi}", "n": len(seg),
                        "avg_pnl": round(sum(pnls) / len(pnls), 2),
                        "win": round(sum(1 for p in pnls if p > 0) / len(pnls), 3)})
        return out

    return {
        "n": len(xs),
        "by_vol": _bucket("vol_ratio", [0, 0.8, 1.0, 1.2, 1.5, 1e9]),
        "by_mom20": _bucket("mom20", [-1e9, 0, 0.10, 0.20, 0.30, 1e9]),
    }


def main() -> int:
    t0 = datetime.now()
    print(f"[sweep] {len(GRID)} 格 3 并发 inner 扫描 + 逐笔画像…")
    with ProcessPoolExecutor(WORKERS) as ex:
        cells = list(ex.map(_cell, GRID))
    for c in cells:
        print(f"  {c['key']}={c['val']:<5} n={c['n_hits']:>6} "
              f"胜率{c['win_rate'] or 0:>6.1%} 均rr{c['avg_rr'] or 0:>6.3f} "
              f"年化{c['annualized_return'] or 0:>7.1%} dd{c['max_drawdown'] or 0:>6.1%}")

    print(f"\n[profile] 逐笔画像（突破日量比 / 20 日涨幅 双维）：")
    prof = _profile()
    print(f"  样本 {prof['n']} 笔；按突破日量比：")
    for b in prof["by_vol"]:
        print(f"    量比 {b['range']:>10} | n={b['n']:>5} 均笔{b['avg_pnl']:>7.2f}% 胜率{b['win']:>6.1%}")
    print(f"  按 20 日涨幅：")
    for b in prof["by_mom20"]:
        print(f"    涨幅 {b['range']:>12} | n={b['n']:>5} 均笔{b['avg_pnl']:>7.2f}% 胜率{b['win']:>6.1%}")

    out = ROOT / "diag" / f"momentum_sweep_{t0:%Y%m%d}.json"
    out.write_text(json.dumps(
        {"generated_at": f"{t0:%Y-%m-%d %H:%M:%S}", "cells": cells,
         "profile": prof, "grid": [list(g) for g in GRID]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[done] {out}（{(datetime.now() - t0).total_seconds():.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
