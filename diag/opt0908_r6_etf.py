# -*- coding: utf-8 -*-
"""R6 扩池战役 ②:ETF 语料(2026-09-08 用户指令)。

etf_daily(amount 单位=千元,与股票湖同纲)→ 近 30 日均额 ≥1 亿元闸 →
replay 六年(同款颈线识别+执行参数)。读数设计(先于看数):
  ① ETF 全池信号量与期望(形态在篮子上的有效性=开放式问题,先看数)
  ② ETF 与双创/主板的时间互补性:ETF 信号日在双创「above 空窗日」的覆盖
  ③ 合池选序(amihud keep-top 在 股票+ETF 横截面的行为)
风险如实呈报:ETF 形态=篮子行为与个股不同族;盘口薄(成交假设失真);
部分跨境 ETF T+0 制度差异;无涨跌停约束差异(A 股 ETF 也 10cm 但双创类 ETF 20cm)。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd

from backtest.replay import replay
from diag.opt0908_lib import FULL, ev
from diag.opt0908_r5_seqgrid import amihud_series, prep
from experiment.resolver import resolve_champion
from strategies.neckline.strategy import NecklineMethodStrategy

OUT = Path("diag/opt0908_etf_corpus.parquet")


def main() -> None:
    t0 = time.time()
    etf = pd.read_parquet("data_lake/etf_daily.parquet",
                          filters=[("date", ">=", pd.Timestamp("2019-06-01"))])
    amt = etf.groupby(level="symbol")["amount"] \
        .apply(lambda s: s.tail(30).mean() if len(s) else 0.0)
    syms = amt[amt >= 1e5].index.tolist()      # 1 亿元闸(千元单位,同股票湖)
    print(f"[etf] 湖内 ETF {amt.shape[0]} 只 → 1 亿闸后 {len(syms)} 只 "
          f"({time.time()-t0:.0f}s)", flush=True)
    uni = {}
    for s in syms:
        try:
            df = etf.xs(s, level="symbol").sort_index()
        except KeyError:
            continue
        if len(df) < 500:                       # 暖机不足
            continue
        uni[s] = df[["open", "high", "low", "close", "volume", "amount"]]
    print(f"[etf] universe={len(uni)} 只(≥500 根) {time.time()-t0:.0f}s", flush=True)
    params = dict(resolve_champion().params)
    rep = replay(dict(uni), NecklineMethodStrategy(cfg_override=params),
                 "2021-01-01", "2026-09-04")
    rows = [{k: t[k] for k in ("symbol", "formed_at", "entry_date", "entry_price",
                               "exit_date", "exit_price", "exit_reason",
                               "holding_bars", "avg_pnl_pct", "neckline",
                               "bottom", "atr")}
            for t in rep.trades]
    tr = pd.DataFrame(rows)
    tr["formed_at"] = pd.to_datetime(tr["formed_at"])
    tr.to_parquet(OUT)
    print(f"[etf] 语料 n={len(tr)} → {OUT} ({time.time()-t0:.0f}s)", flush=True)
    if not len(tr):
        return
    tr = prep(tr)
    print(f"[etf] 全池: 均笔 {tr.avg_pnl_pct.mean():+.2f}% "
          f"胜率 {(tr.avg_pnl_pct > 0).mean():.1%} | 出场 "
          f"{(tr.exit_reason.value_counts(normalize=True) * 100).round(1).to_dict()}",
          flush=True)
    a, d, amin, amax = ev(tr, FULL)
    print(f"[etf] 全池组合(20x5% 直进, 无选序): ann {a:+.1f} dd {d:+.1f} "
          f"cal {a / abs(d):.2f}", flush=True)
    # 时间互补性:ETF 信号日 vs 双创 above 空窗
    idx = pd.read_parquet("data_lake/index_daily.parquet", columns=["close"])
    cyb = idx.xs("399006.SZ", level="symbol")["close"].sort_index()
    ratio = (cyb / cyb.rolling(60).mean())
    ratio = ratio[(ratio.index >= "2021-01-01") & (ratio.index <= "2026-09-04")]
    below_days = ratio[ratio < 1.0].index.normalize()
    etf_days = set(tr.formed_at.dt.normalize())
    cov = len(below_days.intersection(etf_days))
    print(f"[etf] 时间互补: below 区 {len(below_days)} 天中 ETF 有信号 "
          f"{cov} 天({cov / len(below_days):.0%})", flush=True)
    json.dump({"n": int(len(tr)),
               "per_trade": round(float(tr.avg_pnl_pct.mean()), 3),
               "below_cov": round(cov / len(below_days), 3)},
              open("logs/committee_20260908/r6_etf.json", "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
