# -*- coding: utf-8 -*-
"""opt0908_lib —— 会战整改回测共享库（2026-09-08）。

实盘口径三件套单源复用（与 diag/fourway_compare.py 同口径）：
  语料 parquet → amihud60 keep-top5 选序 → PM 20×5% 冻结挂单 →
  随机 21 种子中位（全期）/11 种子（分年）。
用法（各轮整改脚本 import）：
  from diag.opt0908_lib import CORPUS, ev, report_arms, baseline_full
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd

from backtest.models import PositionModel
from discovery.objective import portfolio_metrics
from discovery.split import Segment

_idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
CAL = _idx.xs("000300.SH", level="symbol").index.sort_values()
CYB = _idx.xs("399006.SZ", level="symbol")["close"].sort_index()
_LAKE = pd.read_parquet(
    "data_lake/a_shares_daily.parquet",
    filters=[("date", ">=", pd.Timestamp("2019-06-01"))],
    columns=["close", "amount"])

PM = PositionModel(capital=200_000., pos_cap=.05, max_positions=20,
                   lot_size=0, min_fee=0., freeze_pending=True)
FULL = Segment("f", pd.Timestamp("2021-01-01").date(),
               pd.Timestamp("2026-09-04").date())
Y26 = Segment("y26", pd.Timestamp("2026-01-01").date(),
              pd.Timestamp("2026-09-04").date())
YEARS = [Segment(f"y{y}", pd.Timestamp(f"{y}-01-01").date(),
                 pd.Timestamp(f"{y}-12-31").date()) for y in range(2021, 2027)]


def load_base(path: str) -> pd.DataFrame:
    """语料 → amihud60 keep-top5（按信号日选最不流动 5 只，08-29 裁决口径）。"""
    tr = pd.read_parquet(path)
    tr["formed_at"] = pd.to_datetime(tr["formed_at"])
    syms = set(tr["symbol"].unique())
    lk = _LAKE[_LAKE.index.get_level_values("symbol").isin(syms)]
    ret = lk["close"].groupby(level="symbol").pct_change()
    am = (ret.abs() / (lk["amount"].astype("float32") + 1.0)) \
        .groupby(level="symbol").rolling(60, min_periods=48).mean()
    am = am.reset_index(level=0, drop=True).sort_index()
    tr["amihud60"] = am.reindex(
        pd.MultiIndex.from_arrays([tr["formed_at"], tr["symbol"]])).values

    def kt5(g: pd.DataFrame) -> pd.Series:
        m = pd.Series(True, index=g.index)
        if len(g) <= 5:
            return m
        v = g.dropna(subset=["amihud60"])
        if len(v) < 5:
            return m
        k = v.sort_values("amihud60", ascending=False).head(5).index
        m.loc[g.index.difference(k)] = False
        return m

    tr["sel"] = tr.groupby("formed_at", group_keys=False).apply(kt5)
    return tr[tr["sel"]].copy()


def _to_trades(sub: pd.DataFrame) -> list[dict]:
    return [{"symbol": s, "signal_date": pd.Timestamp(d), "buy_date": b,
             "exit_date": e, "avg_pnl_pct": float(p), "entry": None,
             "priority": None, "pos_cap": None}
            for s, d, b, e, p in zip(sub.symbol, sub.formed_at,
                                     sub.entry_date, sub.exit_date,
                                     sub.avg_pnl_pct)]


def ev(sub: pd.DataFrame, seg: Segment, n: int = 21) -> tuple:
    """(ann中位%, dd中位%, ann_min%, ann_max%)——随机 queue_order 多种子。"""
    trades = _to_trades(sub)
    ms = [portfolio_metrics(trades, seg, CAL,
                            position_model=replace(PM, queue_order="random",
                                                   queue_seed=s))
          for s in range(n)]
    an = np.array([m["ann"] for m in ms])
    dd = np.array([m["max_dd"] for m in ms])
    return float(np.median(an) * 100), float(np.median(dd) * 100), \
        float(an.min() * 100), float(an.max() * 100)


def report_arms(arms: dict[str, pd.DataFrame], *, years_n: int = 11) -> list[dict]:
    """arms: {名称: 语料子集} → 打印+返回结构化行（全期/分年/2026 全套）。"""
    rows = []
    print(f"{'臂':<24}{'n':>6}{'逐笔':>7}{'ann中位':>9}{'ann带':>15}"
          f"{'dd中位':>8}{'2026YTD':>9} | 分年ann 21→26", flush=True)
    for name, sub in arms.items():
        a, d, amin, amax = ev(sub, FULL)
        a26 = ev(sub, Y26, years_n)[0]
        ys = [ev(sub, s, years_n)[0] for s in YEARS]
        cal = a / abs(d) if d else float("nan")
        print(f"{name:<24}{len(sub):>6}{sub.avg_pnl_pct.mean():>+6.2f}%"
              f"{a:>+8.1f}%{amin:>+6.1f}~{amax:<+6.1f}%{d:>+7.1f}%"
              f"{a26:>+8.1f}% | " + " ".join(f"{y:+5.1f}" for y in ys),
              flush=True)
        rows.append({"arm": name, "n": int(len(sub)),
                     "per_trade": round(float(sub.avg_pnl_pct.mean()), 3),
                     "ann_med": round(a, 2), "dd_med": round(d, 2),
                     "calmar": round(cal, 2), "ann_band": [round(amin, 1),
                                                           round(amax, 1)],
                     "y2026": round(a26, 2),
                     "years": [round(y, 1) for y in ys]})
    return rows


CORPUS = None   # lazy：load_corpus() 首次调用后缓存


def load_corpus() -> pd.DataFrame:
    """B3 主腿六年语料（keep-top5 已选），派生列 premium/cyb_ratio 就绪。"""
    global CORPUS
    if CORPUS is None:
        CORPUS = load_base("diag/retrial_corpus_rolling.parquet")
        CORPUS["premium"] = ((CORPUS["entry_price"] - CORPUS["neckline"])
                             / CORPUS["atr"])
        CORPUS["cyb_ratio"] = (CYB / CYB.rolling(60).mean()) \
            .reindex(CORPUS["formed_at"]).values
    return CORPUS


_UNI = None


def replay_corpus(param_delta: dict, end: str = "2026-09-04") -> pd.DataFrame:
    """Type A 参数提案重扫：B3 champion + delta → 六年 replay 语料（keep-top5）。

    识别参数变化吃 R6-1 缓存 miss（~2.5min），exec 参数变化全命中（~15-30s）。
    """
    from backtest.replay import replay as _replay
    from discovery.snapshot import freeze, load_universe
    from experiment.resolver import resolve_champion
    from strategies.neckline.strategy import NecklineMethodStrategy
    global _UNI
    if _UNI is None:
        frozen, _ = freeze("2021-01-01")
        _UNI = {s: df for s, df in
                load_universe(start="2020-01-01").items() if s in frozen}
    params = {**dict(resolve_champion().params), **param_delta}
    rep = _replay(dict(_UNI), NecklineMethodStrategy(cfg_override=params),
                  "2021-01-01", end)
    rows = [{**{k: t[k] for k in ("symbol", "formed_at", "entry_date", "entry_price",
                                  "exit_date", "exit_price", "exit_reason", "rr",
                                  "holding_bars", "avg_pnl_pct", "neckline",
                                  "bottom", "atr")},
             "limit_deferred": t.get("limit_deferred", False)}
            for t in rep.trades]
    tr = pd.DataFrame(rows)
    tr["formed_at"] = pd.to_datetime(tr["formed_at"])
    out = load_base_from_df(tr)
    out["premium"] = ((out["entry_price"] - out["neckline"]) / out["atr"])
    out["cyb_ratio"] = (CYB / CYB.rolling(60).mean()) \
        .reindex(out["formed_at"]).values
    return out


def load_base_from_df(tr: pd.DataFrame) -> pd.DataFrame:
    """replay 语料 DataFrame → amihud keep-top5（不落盘路径版 load_base）。"""
    syms = set(tr["symbol"].unique())
    lk = _LAKE[_LAKE.index.get_level_values("symbol").isin(syms)]
    ret = lk["close"].groupby(level="symbol").pct_change()
    am = (ret.abs() / (lk["amount"].astype("float32") + 1.0)) \
        .groupby(level="symbol").rolling(60, min_periods=48).mean()
    am = am.reset_index(level=0, drop=True).sort_index()
    tr["amihud60"] = am.reindex(
        pd.MultiIndex.from_arrays([tr["formed_at"], tr["symbol"]])).values

    def kt5(g: pd.DataFrame) -> pd.Series:
        m = pd.Series(True, index=g.index)
        if len(g) <= 5:
            return m
        v = g.dropna(subset=["amihud60"])
        if len(v) < 5:
            return m
        k = v.sort_values("amihud60", ascending=False).head(5).index
        m.loc[g.index.difference(k)] = False
        return m

    tr["sel"] = tr.groupby("formed_at", group_keys=False).apply(kt5)
    return tr[tr["sel"]].copy()


def stack_arms(base: pd.DataFrame, guard_skip: float = 3.0,
               guard_t1: bool = True) -> dict[str, pd.DataFrame]:
    """从主腿语料构造四栈臂（纯收益栈=skip；均衡栈=skip+T1）。"""
    skip = base[~(base.premium > guard_skip)]
    arms = {"主腿B3": base, f"纯收益栈skip@{guard_skip}": skip}
    if guard_t1:
        arms[f"均衡栈skip@{guard_skip}+T1"] = skip[skip.cyb_ratio >= 1.0]
    return arms
