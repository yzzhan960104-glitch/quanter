# -*- coding: utf-8 -*-
"""opt0908_pertrade_lib —— 单笔口径评估库(R7+ 会战:胜率×单笔收益目标)。

与组合口径(opt0908_lib)对立:不看并发/占用/年化,只看每一笔的质量。
双窗纪律:inner(2021-24)/outer(2025-26)逐笔分开——防单窗过拟合。
R6-1 识别缓存的进程内红利:exec 参数网格单进程连跑,识别只算一次。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd

from backtest.replay import replay as _replay
from discovery.snapshot import freeze, load_universe
from experiment.resolver import resolve_champion
from strategies.neckline.strategy import NecklineMethodStrategy

INNER_END = pd.Timestamp("2024-12-31")
OUTER_START = pd.Timestamp("2025-01-01")


def per_trade_metrics(tr: pd.DataFrame, tag: str = "") -> dict:
    """逐笔质量五件套+出场结构(全池,skip 不计入——成交才算一笔)。"""
    if not len(tr):
        return {"tag": tag, "n": 0}
    p = tr["avg_pnl_pct"].values
    out = {
        "tag": tag, "n": int(len(tr)),
        "win": round(float((p > 0).mean()) * 100, 1),
        "mean": round(float(p.mean()), 3),
        "median": round(float(np.median(p)), 3),
        "p5": round(float(np.percentile(p, 5)), 2),
        "p95": round(float(np.percentile(p, 95)), 2),
    }
    if "exit_reason" in tr:
        vc = tr["exit_reason"].value_counts(normalize=True)
        out["tp2"] = round(float(vc.get("tp2", 0)) * 100, 1)
        out["stop"] = round(float(vc.get("stop_loss", 0)) * 100, 1)
        out["to"] = round(float(vc.get("timeout", 0)) * 100, 1)
    return out


def dual_window(tr: pd.DataFrame, tag: str = "") -> dict:
    """inner/outer 双窗逐笔+全期(单口径纪律:两窗都要看)。"""
    inner = tr[tr.formed_at <= INNER_END]
    outer = tr[tr.formed_at > OUTER_START]
    return {"full": per_trade_metrics(tr, tag + "|全期"),
            "inner": per_trade_metrics(inner, tag + "|inner21-24"),
            "outer": per_trade_metrics(outer, tag + "|outer25-26")}


def show(rows: list[dict]) -> None:
    print(f"{'臂':<26}{'n':>6}{'胜率':>6}{'均笔':>7}{'中位':>7}{'P5':>7}"
          f"{'P95':>7}{'tp2%':>6}{'stop%':>6}", flush=True)
    for r in rows:
        if r.get("n", 0) == 0:
            print(f"{r['tag']:<26}  0 笔", flush=True)
            continue
        print(f"{r['tag']:<26}{r['n']:>6}{r['win']:>5.1f}%{r['mean']:>+6.2f}%"
              f"{r['median']:>+6.2f}%{r['p5']:>+6.1f}%{r['p95']:>+6.1f}%"
              f"{r.get('tp2', 0):>6.1f}{r.get('stop', 0):>6.1f}", flush=True)


_UNI = None


def universe() -> dict:
    global _UNI
    if _UNI is None:
        frozen, _ = freeze("2021-01-01")
        _UNI = {s: df for s, df in
                load_universe(start="2020-01-01").items() if s in frozen}
    return dict(_UNI)


def replay_trades(param_delta: dict, end="2026-09-04") -> pd.DataFrame:
    """champion + delta → 六年全信号语料(不选序——单笔口径全池)。"""
    params = {**dict(resolve_champion().params), **param_delta}
    rep = _replay(universe(), NecklineMethodStrategy(cfg_override=params),
                  "2021-01-01", end)
    rows = [{**{k: t[k] for k in ("symbol", "formed_at", "entry_date",
                                  "entry_price", "exit_date", "exit_price",
                                  "exit_reason", "holding_bars", "avg_pnl_pct",
                                  "neckline", "bottom", "atr")},
             "limit_deferred": t.get("limit_deferred", False)}
            for t in rep.trades]
    tr = pd.DataFrame(rows)
    tr["formed_at"] = pd.to_datetime(tr["formed_at"])
    return tr


def grid_scan(grids: list[dict], tag_prefix: str = "") -> list[dict]:
    """exec/识别参数网格:单进程连跑吃 R6-1 识别缓存。

    grids: [{tag, delta}, ...] —— 识别参数变会在缓存 miss(慢,~2.5min/格),
    exec 参数变全命中(~15-30s/格)。按「先识别后 exec」排序最优。
    返回逐笔双窗行(含 raw 语料内存,调用方可再深挖)。
    """
    results = []
    for i, g in enumerate(grids):
        tr = replay_trades(g["delta"])
        d = dual_window(tr, f"{tag_prefix}{g['tag']}")
        d["_tr"] = tr
        results.append(d)
        f = d["full"]
        print(f"[grid {i+1}/{len(grids)}] {g['tag']}: n={f['n']} "
              f"win={f['win']}% mean={f['mean']}% | inner {d['inner']['win']}/"
              f"{d['inner']['mean']} | outer {d['outer']['win']}/"
              f"{d['outer']['mean']}", flush=True)
    return results


def scan_trades(param_delta: dict) -> pd.DataFrame:
    """run_full_scan 路径(吃 R6-1 识别缓存:同进程 exec 网格 ~15-30s/格)。"""
    params = {**dict(resolve_champion().params), **param_delta}
    filled = run_full_scan(params, universe())
    tr = pd.DataFrame(filled)
    if not len(tr):
        return tr
    tr["formed_at"] = pd.to_datetime(tr["signal_date"])
    return tr


def scan_trades_full(param_delta: dict):
    """P1 判据基建版:返回 (tr, meta)——meta 含 n_sig/n_skip/skip 率/
    每信号期望(=成交率×成交后期望)+regime 三栏(above/wire/deep)。"""
    params = {**dict(resolve_champion().params), **param_delta}
    id_cfg = {**DEFAULTS_B, **{k: params.get(k, DEFAULTS_B.get(k))
                               for k in ID_KEYS_B}}
    exec_cfg = {**EXEC_DEFAULTS, **{k: params.get(k, EXEC_DEFAULTS.get(k))
                                    for k in EXEC_KEYS_B}}
    uni = universe()
    all_filled, n_sig_total, n_skip_total = [], 0, 0
    for sym, sym_df in uni.items():
        try:
            filled, n_sig, n_skip = scan_symbol(
                sym_df, id_cfg["window"], exec=exec_cfg, id_cfg=id_cfg,
                symbol=sym)
        except Exception:
            continue
        all_filled.extend(filled)
        n_sig_total += n_sig
        n_skip_total += n_skip
    tr = pd.DataFrame(all_filled)
    if len(tr):
        tr["formed_at"] = pd.to_datetime(tr["signal_date"])
    fill_rate = len(tr) / n_sig_total if n_sig_total else 0.0
    mean_fill = float(tr["avg_pnl_pct"].mean()) if len(tr) else 0.0
    meta = {"n_sig": n_sig_total, "n_fill": len(tr), "n_skip": n_skip_total,
            "skip_rate": round(1 - fill_rate, 3),
            "per_signal_exp": round(fill_rate * mean_fill, 4)}
    # regime 三栏(决策时合法:formed_at 日的指数 MA60 比率)
    idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
    cyb = idx.xs("399006.SZ", level="symbol")["close"].sort_index()
    ratio = cyb / cyb.rolling(60).mean()
    if len(tr):
        r = ratio.reindex(tr["formed_at"]).values
        tr["cyb_ratio"] = r
        import numpy as _np
        r = _np.where(_np.isnan(r), -9.0, r)
        for zone, m in (("above", r >= 1.0), ("wire", (r >= 0.97) & (r < 1.0)),
                        ("deep", r < 0.97)):
            sub = tr[m]
            meta[f"{zone}"] = per_trade_metrics(sub, zone) if len(sub) else {"n": 0}
    return tr, meta


# run_full_scan 同款白名单(避免循环 import,延迟绑定)
from strategies.neckline.backtest import DEFAULTS as DEFAULTS_B  # noqa: E402
from strategies.neckline.backtest import EXEC_DEFAULTS  # noqa: E402
from discovery.objective import EXEC_KEYS as EXEC_KEYS_B  # noqa: E402
from discovery.objective import ID_KEYS as ID_KEYS_B  # noqa: E402
from strategies.neckline.backtest import scan_symbol  # noqa: E402
