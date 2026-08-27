# -*- coding: utf-8 -*-
"""R4 首轮止损解剖：逐笔归因驱动优化的数据地基（2026-08-24 · 用户范式裁决）。

物理意图（方法论转向，ROUND_LOG R3→R4 节录）：
    用户裁决——不从全局广度搜索出「哪组参数好」，而是**深入每一次止损的买入
    行为**，分析为什么止损，以局部最优逼近全局最优。本脚本做第一步：把止损单
    的「死法」特征化，与止盈组对照（只看止损不设对照会得出「入场即原罪」的
    错误结论——判别力来自组间差）。

归因特征（每笔，两类）：
    几何（识别侧，来自 Signal）：H/ATR=(neckline−bottom)/atr 形态深度、
        rr 预期盈亏比、entry_gap=(entry_price−neckline)/atr 挂单离颈线距离、
        wait_days=entry−formed 等待回踩天数、holding_bars 持有期；
    环境（市场侧，池子等权现算）：mom20=入场日池子 20 日收益、vol20=20 日
        日收益波动率——区分「形态自己死」vs「市场带着死」。

防过拟合纪律（R4 军规，逐笔范式版信息隔离）：
    同一死法特征必须在 ≥3 个年段同向复现才可立项改进（单年段的止损特征
    多为噪声）；年段切片全部落盘供检验。

用法：PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -u diag/r4_stop_autopsy.py
产物：logs/r4_stop_autopsy.json + 控制台分类学表。
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

import pandas as pd

from discovery.snapshot import freeze
from backtest.replay import replay
from strategies.neckline.strategy import NecklineMethodStrategy
from discovery.manual_risk_sim import _pool_equity

PARAM_SETS = {}
_ec = sqlite3.connect("experiment/experiments.db")
PARAM_SETS["active_25c602"] = json.loads(_ec.execute(
    "SELECT params FROM experiment_version WHERE status='ACTIVE' LIMIT 1").fetchone()[0])
_tc = sqlite3.connect("file:logs/discovery_trials.db?mode=ro", uri=True)
PARAM_SETS["r3_49e3755"] = json.loads(_tc.execute(
    "SELECT params FROM trial WHERE trial_id='49e3755b322c'").fetchone()[0])


def _features(trades: list, pool_daily: pd.Series, mom20: pd.Series, vol20: pd.Series) -> pd.DataFrame:
    rows = []
    for t in trades:
        if t.get("neckline") is None or t.get("atr") in (None, 0):
            continue
        ed = pd.Timestamp(t["entry_date"])
        fd = pd.Timestamp(t["formed_at"]) if t.get("formed_at") else ed
        h_atr = (t["neckline"] - (t["bottom"] if t["bottom"] is not None else t["neckline"])) / t["atr"]
        rows.append({
            "exit_reason": t["exit_reason"] or "unknown",
            "year": ed.year,
            "rr": float(t["rr"] or 0),
            "pnl": float(t["avg_pnl_pct"] or 0),
            "h_atr": float(h_atr),
            "entry_gap_atr": float((t["entry_price"] - t["neckline"]) / t["atr"]),
            "wait_days": float((ed - fd).days),
            "holding": float(t["holding_bars"] or 0),
            "mom20": float(mom20.reindex([ed], method="ffill").iloc[0]),
            "vol20": float(vol20.reindex([ed], method="ffill").iloc[0]),
        })
    return pd.DataFrame(rows)


def _profile(df: pd.DataFrame, group: str) -> dict:
    g = df[df["exit_reason"] == group]
    if g.empty:
        return {}
    keys = ("rr", "h_atr", "entry_gap_atr", "wait_days", "holding", "mom20", "vol20")
    return {"n": len(g), **{k: round(float(g[k].mean()), 3) for k in keys}}


def main() -> int:
    universe, meta = freeze("2021-01-01")
    pool = _pool_equity(universe)
    daily = pool.pct_change()
    mom20 = pool.pct_change(20)
    vol20 = daily.rolling(20).std()

    out = {"snapshot": meta.snapshot_hash, "sets": {}}
    for name, params in PARAM_SETS.items():
        t0 = time.time()
        strat = NecklineMethodStrategy(cfg_override=params)
        rep = replay(universe, strat, "2021-01-01", "2026-08-21")
        df = _features(rep.trades, daily, mom20, vol20)
        # 分类学总表
        groups = {}
        for g in df["exit_reason"].unique():
            groups[g] = _profile(df, g)
        # 止损 vs 止盈（tp1/tp2 合并）判别力：均值差与方向
        stop = df[df["exit_reason"] == "stop_loss"]
        tp = df[df["exit_reason"].isin(("tp1", "tp2"))]
        disc = {}
        for k in ("rr", "h_atr", "entry_gap_atr", "wait_days", "mom20", "vol20"):
            if stop.empty or tp.empty:
                break
            disc[k] = {"stop": round(float(stop[k].mean()), 3),
                       "tp": round(float(tp[k].mean()), 3),
                       "sep": round(float(stop[k].mean() - tp[k].mean()), 3)}
        # 年段切片：止损组特征逐年（跨段复现检验——≥3 年段同向才立项）
        by_year = {}
        for y, g in stop.groupby("year"):
            by_year[int(y)] = {k: round(float(g[k].mean()), 3)
                               for k in ("rr", "h_atr", "entry_gap_atr", "mom20", "vol20")}
        out["sets"][name] = {"groups": groups, "discriminate": disc,
                             "stop_by_year": by_year}
        print(f"\n=== {name}（{len(df)} 笔，用 {(time.time()-t0)/60:.0f}min）", flush=True)
        for g, p in groups.items():
            print(f"  {g:>10}: n={p.get('n', 0):>4} rr={p.get('rr', 0):+.2f} H/ATR={p.get('h_atr', 0):.2f} "
                  f"gap={p.get('entry_gap_atr', 0):+.2f} wait={p.get('wait_days', 0):.0f}d "
                  f"mom20={p.get('mom20', 0):+.2%} vol20={p.get('vol20', 0):.2%}", flush=True)
        if disc:
            print("  止损 vs 止盈 判别（正=止损侧更高）:", flush=True)
            for k, d in disc.items():
                print(f"    {k:>13}: stop {d['stop']:>8} vs tp {d['tp']:>8}（Δ{d['sep']:+}）", flush=True)

    with open("logs/r4_stop_autopsy.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print("\n[done] logs/r4_stop_autopsy.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
