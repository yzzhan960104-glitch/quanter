# -*- coding: utf-8 -*-
"""溢价分层过滤（B 档策略增强候选）完整回测（2026-09-04 用户指令）。

背景：buy_limit_sweep 逐笔实证——最高溢价档（>2.5×ATR）均笔收益只有贴颈线
档的 46%；但全局收紧 N 上限会误杀浅回踩高动量信号（年化反降）。正确形态=
执行时分层：按**实际成交溢价**过滤。

变体矩阵：mode ∈ {skip 跳过, half 减半仓} × thresh ∈ {2.0, 2.5, 3.0}×ATR
窗口：inner=2025 / outer=2026（holdout_split 同提案验证口径）
方法：逐笔重放模拟——基线 replay 拿全部逐笔 → 变体变换 trades →
build_equity_curve（**同款资金模型含并发/现金约束**，skip 释放的并发额度
自动可被后续笔利用=资金再利用语义正确）→ CAGR/dd 与 signal 级指标聚合。

口径注记：
  - half 变体=rr/avg_pnl_pct 减半（仓位减半的一阶近似；忽略费率二阶项）
  - 溢价>buy_limit(2.5)+0.01×ATR 的笔=chase 追入单（挂单成交价不可能超
    挂单价）——报告单列 chase 笔占比，过滤的主要对象
  - 与正式 B 档实施的差异：信号级过滤（entry 前判定） vs 本模拟（exit 后
    重放）；并发额度释放时序在边界笔有一根 K 线的重叠误差——一阶近似

CLI：python diag/premium_filter_sim.py
"""
from __future__ import annotations

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

MODES = ("skip", "half")
THRESHES = (2.0, 2.5, 3.0)
BASE_N = 2.5                                  # 冠军 buy_limit_atr_mult


def _baseline_trades(seg: str):
    """基线 replay 拿逐笔+窗口交易日数（段边界=holdout_split 同提案口径）。"""
    from backtest.replay import replay
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from experiment.resolver import resolve_champion
    from strategies.neckline.strategy import NecklineMethodStrategy

    base = dict(resolve_champion().params)
    split = holdout_split()
    universe, _ = freeze("2025-01-01")
    seg_range = (split.inner if seg == "inner" else split.outer)
    strat = NecklineMethodStrategy(cfg_override=base)
    rep = replay(universe, strat, str(seg_range.start), str(seg_range.end))
    trades = []
    for t in rep.trades:
        e, n, a = t.get("entry_price"), t.get("neckline"), t.get("atr")
        if not e or not n or not a or a <= 0 or t.get("rr") is None:
            continue
        trades.append({**t, "prem": (e - n) / a})
    return trades, rep.n_trading_days, base


def _simulate(trades: list[dict], n_days: int, mode: str, thresh: float) -> dict:
    """变体变换 → 组合口径（equity/CAGR/dd）+ signal 口径聚合。"""
    from backtest.models import PositionModel, build_equity_curve

    if mode == "skip":
        kept = [t for t in trades if t["prem"] <= thresh]
        dropped = [t for t in trades if t["prem"] > thresh]
    else:                                        # half：高溢价笔仓位减半
        kept, dropped = [], []
        for t in trades:
            if t["prem"] > thresh:
                kept.append({**t, "rr": t["rr"] * 0.5,
                             "avg_pnl_pct": (t.get("avg_pnl_pct") or 0) * 0.5})
                dropped.append(t)
            else:
                kept.append(t)
    curve = build_equity_curve(kept, PositionModel())
    eq_end = float(curve[-1]["equity"]) if curve else 1.0
    cagr = eq_end ** (252.0 / n_days) - 1.0 if n_days and eq_end > 0 else 0.0
    dd, peak = 0.0, 1.0
    for p in curve:
        eq = float(p["equity"])
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1.0)
    rrs = [t["rr"] for t in kept]
    n_dropped, drop_pnl = len(dropped), [t.get("avg_pnl_pct") or 0 for t in dropped]
    return {
        "n_kept": len(kept), "n_dropped": n_dropped,
        "win_rate": round(sum(1 for r in rrs if r > 0) / len(rrs), 3) if rrs else 0,
        "avg_rr": round(sum(rrs) / len(rrs), 3) if rrs else 0,
        "cagr": round(cagr, 3), "max_dd": round(dd, 3),
        "dropped_avg_pnl": round(sum(drop_pnl) / len(drop_pnl), 2) if drop_pnl else None,
    }


def main() -> int:
    t0 = datetime.now()
    results = {}
    for seg in ("inner", "outer"):
        trades, n_days, base = _baseline_trades(seg)
        chase = [t for t in trades if t["prem"] > BASE_N + 0.01]
        print(f"\n=== {seg}（基线 {len(trades)} 笔，{n_days} 交易日）===")
        print(f"  基线对照: 见下表 no-filter 行")
        print(f"  chase 追入笔（溢价>{BASE_N}×ATR）: {len(chase)} 只 "
              f"({len(chase)/len(trades):.1%})，均笔 "
              f"{sum(t.get('avg_pnl_pct') or 0 for t in chase)/max(1, len(chase)):.2f}%")
        rows = {}
        rows["no-filter"] = _simulate(trades, n_days, "skip", 1e9)
        for mode in MODES:
            for th in THRESHES:
                rows[f"{mode}@{th}"] = _simulate(trades, n_days, mode, th)
        hdr = f"  {'变体':<12} {'笔数':>6} {'弃笔':>5} {'胜率':>6} {'均rr':>6} " \
              f"{'年化':>7} {'回撤':>6} 弃笔均笔"
        print(hdr)
        for k, v in rows.items():
            print(f"  {k:<12} {v['n_kept']:>6} {v['n_dropped']:>5} "
                  f"{v['win_rate']:>6.1%} {v['avg_rr']:>6.3f} "
                  f"{v['cagr']:>7.1%} {v['max_dd']:>6.1%} "
                  f"{str(v['dropped_avg_pnl']) + '%' if v['dropped_avg_pnl'] is not None else '—':>8}")
        results[seg] = {"n_trades": len(trades), "n_days": n_days,
                        "chase_n": len(chase), "chase_share": round(len(chase)/len(trades), 3),
                        "rows": rows}

    out = ROOT / "diag" / f"premium_filter_sim_{t0:%Y%m%d}.json"
    out.write_text(json.dumps(
        {"generated_at": f"{t0:%Y-%m-%d %H:%M:%S}", **results,
         "note": "溢价分层过滤逐笔重放回测：skip/half×2.0/2.5/3.0，"
                 "equity=同款 PositionModel（并发/现金约束，skip 释放额度可复用）"},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[done] {out}（{(datetime.now() - t0).total_seconds():.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
