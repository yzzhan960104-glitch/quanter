# -*- coding: utf-8 -*-
"""R2 整改:P3 deep 区 MA200 广度 sizing + P2 占用分层 sizing(两段式近似)。

共同管道:逐笔 pos_cap(quality_alloc 模式,backtest/models.py:193)+ 实盘口径
三件套(opt0908_lib)。判据预登记(R2 PM 协议,先于看数):
  P3: dd 不劣 T1 且 2025 保留≥90%(vs T1 64.6%),任一年 dd 劣化>2pp 即弃;
      τ∈{0.3,0.4,0.5} 平台性检验不求尖峰。
  P2: dd 改善为主、ann 小幅让渡;thresh∈{0.5,0.6,0.7} 平台性。
"""
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd

from diag.opt0908_lib import CAL, PM, YEARS, Y26, load_corpus
from discovery.objective import portfolio_metrics
from discovery.split import Segment

FULL = Segment("f", pd.Timestamp("2021-01-01").date(),
               pd.Timestamp("2026-09-04").date())


def build_breadth() -> pd.Series:
    """全信号池(60,818 笔,选序前)按信号日的「个股站上自身 MA200 比例」。

    决策时合法性:formed_at 收盘截面(与 heat_day 同型)。
    """
    tr = pd.read_parquet("diag/retrial_corpus_rolling.parquet")
    tr["formed_at"] = pd.to_datetime(tr["formed_at"])
    lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                           filters=[("date", ">=", pd.Timestamp("2019-06-01"))],
                           columns=["close"])
    lake = lake[lake.index.get_level_values("symbol").isin(set(tr.symbol))]
    ma200 = lake["close"].groupby(level="symbol") \
        .rolling(200, min_periods=150).mean()
    ma200 = ma200.reset_index(level=0, drop=True).sort_index()
    above = (lake["close"] > ma200).astype("float32")
    tr["above200"] = above.reindex(
        pd.MultiIndex.from_arrays([tr.formed_at, tr.symbol])).values
    b = tr.groupby("formed_at")["above200"].mean()
    return b


def ev_poscap(sub: pd.DataFrame, seg: Segment, n: int = 21) -> tuple:
    """逐笔 pos_cap 版 ev(quality_alloc)。sub 需含 'pcap' 列(0.05 或减半)。"""
    trades = [{"symbol": s, "signal_date": pd.Timestamp(d), "buy_date": b,
               "exit_date": e, "avg_pnl_pct": float(p), "entry": None,
               "priority": None, "pos_cap": float(pc)}
              for s, d, b, e, p, pc in zip(sub.symbol, sub.formed_at,
                                           sub.entry_date, sub.exit_date,
                                           sub.avg_pnl_pct, sub.pcap)]
    pmm = replace(PM, queue_order="random", quality_alloc=True)
    ms = [portfolio_metrics(trades, seg, CAL,
                            position_model=replace(pmm, queue_seed=s))
          for s in range(n)]
    an = np.array([m["ann"] for m in ms])
    dd = np.array([m["max_dd"] for m in ms])
    return float(np.median(an) * 100), float(np.median(dd) * 100), \
        float(an.min() * 100), float(an.max() * 100)


def report(arms: dict[str, pd.DataFrame]) -> list[dict]:
    rows = []
    print(f"{'臂':<26}{'n':>6}{'ann中位':>9}{'ann带':>15}{'dd中位':>8}"
          f"{'2026YTD':>9} | 分年ann 21→26", flush=True)
    for name, sub in arms.items():
        a, d, amin, amax = ev_poscap(sub, FULL)
        a26 = ev_poscap(sub, Y26, 11)[0]
        ys = [ev_poscap(sub, s, 11)[0] for s in YEARS]
        print(f"{name:<26}{len(sub):>6}{a:>+8.1f}%{amin:>+6.1f}~{amax:<+6.1f}%"
              f"{d:>+7.1f}%{a26:>+8.1f}% | " + " ".join(f"{y:+5.1f}" for y in ys),
              flush=True)
        rows.append({"arm": name, "n": int(len(sub)), "ann_med": round(a, 2),
                     "dd_med": round(d, 2),
                     "calmar": round(a / abs(d), 2) if d else None,
                     "y2026": round(a26, 2), "years": [round(y, 1) for y in ys]})
    return rows


def occupancy_days(sub: pd.DataFrame, thresh: float) -> pd.Timestamp:
    """两段式占用近似:基线 T1 的持仓日历 → 占用>thresh×20 的日期集。"""
    days = pd.DatetimeIndex([])
    for _, t in sub.iterrows():
        days = days.append(pd.date_range(pd.Timestamp(t.entry_date),
                                         pd.Timestamp(t.exit_date)))
    vc = pd.Series(days).value_counts()
    return set(vc[vc > 20 * thresh].index)


def main() -> None:
    base = load_corpus()
    skip = base[~(base.premium > 3.0)]
    t1 = skip[skip.cyb_ratio >= 1.0].copy()
    t1["pcap"] = 0.05
    breadth = build_breadth()

    # ── P3:deep 区广度 sizing(τ 网格)──
    arms: dict[str, pd.DataFrame] = {"T1 基线(pcap5%)": t1}
    for tau in (0.3, 0.4, 0.5):
        s = t1.copy()
        # 广度映射 + 触发条件:deep 区(cyb<0.97)且当日广度<τ
        br = breadth.reindex(s.formed_at).values
        hit = (s.cyb_ratio.values < 0.97) & (br < tau)
        s["pcap"] = np.where(hit, 0.025, 0.05)
        arms[f"P3 deep广度<τ={tau} 半仓"] = s
        print(f"  P3 τ={tau}: 触发 {int(hit.sum())} 笔 / deep 笔 "
              f"{int((s.cyb_ratio.values < 0.97).sum())}", flush=True)
    # ── P2:占用分层(thresh 网格,两段式)──
    occ = occupancy_days(t1, 0.0)  # 先算 vc
    for th in (0.5, 0.6, 0.7):
        hi = occupancy_days(t1, th)
        s = t1.copy()
        ed = pd.to_datetime(s.entry_date)
        s["pcap"] = np.where(ed.isin(hi), 0.025, 0.05)
        arms[f"P2 占用>{int(th*100)}% 新仓半仓"] = s
        print(f"  P2 th={th}: 高占用日 {len(hi)} 天", flush=True)

    rows = report(arms)
    Path("logs/committee_20260908").mkdir(parents=True, exist_ok=True)
    json.dump(rows, open("logs/committee_20260908/r2_p2p3_arms.json", "w",
                         encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
