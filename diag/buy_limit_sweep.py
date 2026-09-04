# -*- coding: utf-8 -*-
"""挂单价（buy_limit_atr_mult）溢价吞噬利润推演·回测（2026-09-04 用户问题）。

问题：挂单价=颈线+N×ATR（当前冠军 N=2.5），追高溢价是否在整体层面吞噬利润。

三段实证：
  ① 全谱扫描：N ∈ {1.0..3.5}（冠军其余参数持住），inner=2025 全市场
     evaluate_replay——年化/胜率/均rr/信号数随 N 的单调性；
  ② outer=2026 复核：关键格（扫描最优 vs 基线）外样本；
  ③ 逐笔溢价-收益相关性：基线参数直接 replay 拿逐笔流水，按
     进场溢价（entry−颈线，%/ATR 归一）分桶看均笔收益/胜率——
     「溢价高的笔是否赚得少」的直接答案。

口径注记：回测按挂单价成交（每笔以最高允许价成交）；实盘 marketable limit
贴盘口成交（常低于挂单价）——回测是溢价伤害的**上界**口径。

CLI：python diag/buy_limit_sweep.py [--full]（--full 跑 outer 复核，默认只 inner）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

GRID = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.5]
BASE_N = 2.5                                  # ACTIVE 冠军实值（params_snapshot）


def _setup():
    from experiment.resolver import resolve_champion
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    champ = resolve_champion()
    assert champ is not None, "无 ACTIVE 冠军"
    split = holdout_split()
    universe, _ = freeze("2025-01-01")
    return dict(champ.params), split, universe


def _evaluate(params, universe, split, seg="inner"):
    from discovery.objective import evaluate_replay
    kw = ({"start": str(split.inner.start), "end": str(split.inner.end)}
          if seg == "inner" else {})
    res = evaluate_replay(params, universe, split, **kw) if seg == "inner" else \
        evaluate_replay(params, universe, split,
                        start=str(split.outer.start), end=str(split.outer.end))
    return res.get(seg) or {}


def sweep() -> tuple[list[dict], dict]:
    base_params, split, universe = _setup()
    print(f"[sweep] 冠军={base_params.get('experiment_id', '?')} "
          f"buy_limit_atr_mult 基线 {BASE_N}，{len(GRID)} 格 inner 扫描…")
    rows = []
    for n in GRID:
        params = {**base_params, "buy_limit_atr_mult": n}
        m = _evaluate(params, universe, split, "inner")
        rows.append({"n": n, **{k: m.get(k) for k in
                                ("n_hits", "win_rate", "avg_rr",
                                 "annualized_return", "max_drawdown")}})
        print(f"  N={n:<5} n={m.get('n_hits'):>6} 胜率{m.get('win_rate', 0):>6.1%} "
              f"均rr{m.get('avg_rr', 0):>6.3f} 年化{m.get('annualized_return', 0):>7.1%} "
              f"dd{m.get('max_drawdown', 0):>6.1%}")
    return rows, {"params": base_params, "split": {"inner": str(split.inner),
                                                   "outer": str(split.outer)}}


def per_trade() -> list[dict]:
    """基线参数逐笔流水 → 溢价桶（ATR 归一：ATR=(颈线−止损)/stop_mult，
    止损经 risk_pct 反推：risk_pct=(entry−stop)/entry）。"""
    from backtest.replay import replay
    from strategies.neckline.strategy import NecklineMethodStrategy
    base_params, split, universe = _setup()
    strat = NecklineMethodStrategy(cfg_override=base_params)
    rep = replay(universe, strat, str(split.inner.start), str(split.inner.end))
    stop_mult = float(base_params.get("stop_atr_mult", 1.5))
    trades = []
    for t in rep.trades:
        entry = t.get("entry")
        neck = t.get("neckline")
        if not entry or not neck or t.get("exit_reason") in ("skip_target_met",
                                                             "skip_no_pullback"):
            continue
        risk_pct = t.get("risk_pct")
        atr = None
        if risk_pct:
            stop = entry * (1 - risk_pct)
            atr = (neck - stop) / stop_mult
        prem_atr = (entry - neck) / atr if (atr and atr > 0) else None
        trades.append({
            "symbol": t.get("symbol") or t.get("ts_code") or "?",
            "entry": entry, "neckline": neck,
            "prem_pct": round((entry / neck - 1) * 100, 2),
            "prem_atr": round(prem_atr, 2) if prem_atr is not None else None,
            "pnl_pct": t.get("avg_pnl_pct"),
            "exit": t.get("exit_reason"),
            "date": str(t.get("signal_date") or t.get("entry_date") or ""),
        })
    return trades


def bucket(trades: list[dict], key: str = "prem_atr", q: int = 5) -> list[dict]:
    """按溢价分 q 桶：均笔收益/胜率/笔数（None 溢度剔除）。"""
    xs = sorted([t for t in trades if t.get(key) is not None
                 and t.get("pnl_pct") is not None], key=lambda t: t[key])
    if not xs:
        return []
    n = len(xs)
    out = []
    for i in range(q):
        seg = xs[i * n // q:(i + 1) * n // q] or xs[-1:]
        pnls = [t["pnl_pct"] for t in seg]
        out.append({
            "range": f"{seg[0][key]}~{seg[-1][key]}",
            "n": len(seg),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "win": round(sum(1 for p in pnls if p > 0) / len(pnls), 3),
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="加跑 outer 复核")
    args = ap.parse_args(argv)
    t0 = datetime.now()

    rows, meta = sweep()

    print("[per-trade] 基线逐笔溢价分桶…")
    trades = per_trade()
    buckets_atr = bucket(trades, "prem_atr")
    print(f"  逐笔 {len(trades)} 笔（按溢价 ATR 倍数五分位）：")
    for b in buckets_atr:
        print(f"    溢价 {b['range']:>10}×ATR | n={b['n']:>5} 均笔{b['avg_pnl']:>7.2f}% "
              f"胜率{b['win']:>6.1%}")

    outer_rows = []
    if args.full:
        base_params, split, universe = _setup()
        best = max(rows, key=lambda r: r.get("annualized_return") or 0)
        for label, n in (("基线", BASE_N), ("最优", best["n"])):
            m = _evaluate({**base_params, "buy_limit_atr_mult": n},
                          universe, split, "outer")
            outer_rows.append({"label": label, "n": n,
                               **{k: m.get(k) for k in ("n_hits", "win_rate",
                                                        "avg_rr",
                                                        "annualized_return")}})
            print(f"  outer {label} N={n}: n={m.get('n_hits')} "
                  f"年化{m.get('annualized_return', 0):.1%} "
                  f"胜率{m.get('win_rate', 0):.1%}")

    doc = {"generated_at": f"{t0:%Y-%m-%d %H:%M:%S}", "grid": rows,
           "meta": meta, "per_trade_buckets_atr": buckets_atr,
           "n_trades": len(trades), "outer": outer_rows,
           "note": "挂单价溢价推演回测：N 扫描（其余冠军参数持住，inner=2025 "
                   "全市场）+基线逐笔溢价分桶；回测按挂单价成交=溢价伤害上界"}
    out = ROOT / "diag" / f"buy_limit_sweep_{t0:%Y%m%d}.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] {out}（{(datetime.now() - t0).total_seconds():.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
