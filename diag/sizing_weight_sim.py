# -*- coding: utf-8 -*-
"""仓位加权模拟（2026-09-04 马拉松·主进程线）：动量区间 sizing。

上轮结论：20 日涨幅 0-10% 组（45% 笔数）均笔最差 3.54%、20-30% 最肥 5.47%——
二值过滤已被否（笔数坍缩），验证「加权而非过滤」：按区间调单笔仓位
（pos_cap 乘数 w∈[0.5, 1.0]），rr/pnl 等比缩放（仓位加权一阶口径），
build_equity_curve 重算（并发约束下低配笔少占额度=可多持其他笔，
加权还有间接的资金效率收益——模拟器天然捕捉）。

同时做持有期画像（holding_bars 分桶）为批③出场结构提供判例。
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


def _mom20(t, by_sym):
    import pandas as pd
    sym, bd = t.get("symbol"), t.get("breakout_date") or t.get("entry_date")
    g = by_sym.get(sym)
    if g is None or not bd:
        return None
    try:
        idx = g.index.get_level_values("date").get_loc(str(bd)[:10])
    except KeyError:
        return None
    if idx < 20:
        return None
    return float(g["close"].iloc[idx] / g["close"].iloc[idx - 20] - 1)


def _metrics(trades, n_days):
    from backtest.models import PositionModel, build_equity_curve
    curve = build_equity_curve(trades, PositionModel())
    eq = float(curve[-1]["equity"]) if curve else 1.0
    cagr = eq ** (252.0 / n_days) - 1.0 if n_days and eq > 0 else 0.0
    dd, peak = 0.0, 1.0
    for p in curve:
        e = float(p["equity"])
        peak = max(peak, e)
        dd = min(dd, e / peak - 1.0)
    return {"cagr": round(cagr, 3), "max_dd": round(dd, 3)}


def main() -> int:
    import pandas as pd
    from backtest.replay import replay
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from experiment.resolver import resolve_champion
    from strategies.neckline.strategy import NecklineMethodStrategy

    t0 = datetime.now()
    base = dict(resolve_champion().params)
    split = holdout_split()
    universe, _ = freeze("2025-01-01")
    strat = NecklineMethodStrategy(cfg_override=base)
    rep = replay(universe, strat, str(split.inner.start), str(split.inner.end))
    trades = [t for t in rep.trades if t.get("rr") is not None]
    n_days = rep.n_trading_days

    df = pd.read_parquet(ROOT / "data_lake" / "a_shares_daily.parquet",
                         columns=["close"])
    by_sym = {s: g.sort_index() for s, g in df.groupby(level="symbol")}
    for t in trades:
        t["_mom20"] = _mom20(t, by_sym)
    xs = [t for t in trades if t["_mom20"] is not None]
    print(f"样本 {len(xs)}/{len(trades)} 笔（{n_days} 交易日）")

    def apply_w(trades, w_low, w_mid, w_high):
        """动量三区仓位乘数：<10% / 10-30% / >30% → 加权 rr/pnl。"""
        out = []
        for t in trades:
            m = t["_mom20"]
            w = w_low if m < 0.10 else (w_high if m > 0.30 else w_mid)
            out.append({**t, "rr": t["rr"] * w,
                        "avg_pnl_pct": (t.get("avg_pnl_pct") or 0) * w})
        return out

    schemes = {
        "基线(全1.0)": (1.0, 1.0, 1.0),
        "低减0.7": (0.7, 1.0, 1.0),
        "低减0.5": (0.5, 1.0, 1.0),
        "低减+肥加": (0.6, 1.2, 0.8),
        "低减0.7+极透减半": (0.7, 1.0, 0.5),
        "温和(0.85,1.1,0.7)": (0.85, 1.1, 0.7),
    }
    print("\n动量区间仓位加权（<10% / 10-30% / >30% 乘数 → inner 年化）：")
    results = {}
    for name, (wl, wm, wh) in schemes.items():
        m = _metrics(apply_w(xs, wl, wm, wh), n_days)
        results[name] = {"w": [wl, wm, wh], **m}
        print(f"  {name:<22} 年化{m['cagr']:>7.1%} dd{m['max_dd']:>6.1%}")

    # 持有期画像（出场结构判例）
    print("\n持有期画像（holding_bars 分桶）：")
    hb = {}
    for t in xs:
        b = t.get("holding_bars") or 0
        k = ("1-3" if b <= 3 else "4-7" if b <= 7 else "8-15" if b <= 15
             else "16-30" if b <= 30 else "31+")
        hb.setdefault(k, []).append(t)
    holding_profile = {}
    for k in ("1-3", "4-7", "8-15", "16-30", "31+"):
        seg = hb.get(k) or []
        if not seg:
            continue
        pnls = [t.get("avg_pnl_pct") or 0 for t in seg]
        exits = {}
        for t in seg:
            exits[t.get("exit_reason")] = exits.get(t.get("exit_reason"), 0) + 1
        top_exits = sorted(exits.items(), key=lambda kv: -kv[1])[:2]
        holding_profile[k] = {
            "n": len(seg), "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "win": round(sum(1 for p in pnls if p > 0) / len(pnls), 3),
            "top_exits": top_exits}
        print(f"  持有{k:>5}日 | n={len(seg):>5} 均笔{sum(pnls)/len(pnls):>7.2f}% "
              f"胜率{holding_profile[k]['win']:>6.1%} 主出场{top_exits}")

    out = ROOT / "diag" / f"sizing_sim_{t0:%Y%m%d}.json"
    out.write_text(json.dumps(
        {"generated_at": f"{t0:%Y-%m-%d %H:%M:%S}", "sizing": results,
         "holding_profile": holding_profile, "n": len(xs)},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[done] {out} ({(datetime.now() - t0).total_seconds():.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
