# -*- coding: utf-8 -*-
"""R6 扩池战役:沪深全市场(60/68/00/30 主板+双创,同 1 亿闸,排 ETF/北交所)
六年 replay 语料 + 实盘口径评估(2026-09-08 用户指令「主板+ETF 放进来」)。

读数设计(先于看数):
  ① 选序构成解剖:kt5/kt7 amihud 选出的信号按板块前缀统计(双创 vs 主板)——
     amihud「最不流动优先」在全市场横截面上天然偏向谁?
  ② 分板块期望:主板票均笔/胜率 vs 双创票(B3 参数从未在主板调过,形态适配风险)
  ③ 全市场 T1 臂(kt5/kt7)vs 双创基线:ann/dd/calmar
  ④ 信号日历密度:above 区满额日占比变化(扩池的核心目标=时间维度补信号)
已知风险(如实呈报,不拦截):回测引擎不建模涨跌停——主板 10cm 边界日的成交
假设失真比双创 20cm 更大(R5b 红旗族);主板票 frozen universe 幸存者口径同既有读数。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd

# ── 池扩展:monkeypatch is_target_board → 全市场 A 股(排北交所/ETF 不在股票湖)──
import discovery.snapshot as SNAP


def _all_a_boards(sym: str) -> bool:
    s = str(sym)
    return (s.startswith(("60", "68", "00")) or s.startswith("30")) \
        and not s.startswith(("8", "4", "92"))


_IS_TARGET_ORIG = SNAP.is_target_board
SNAP.is_target_board = _all_a_boards

from backtest.replay import replay  # noqa: E402  (import 在 patch 后——bind 真函数)
from diag.opt0908_lib import CAL, CYB, PM, FULL, YEARS, ev  # noqa: E402
from diag.opt0908_r5_seqgrid import amihud_series, select_seq, prep  # noqa: E402
from experiment.resolver import resolve_champion  # noqa: E402
from strategies.neckline.strategy import NecklineMethodStrategy  # noqa: E402

OUT = Path("diag/opt0908_fullmkt_corpus.parquet")


def main() -> None:
    t0 = time.time()
    params = dict(resolve_champion().params)
    uni = SNAP.load_universe(start="2020-01-01")
    print(f"[fullmkt] universe={len(uni)} 只(全市场 1 亿闸) {time.time()-t0:.0f}s",
          flush=True)
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
    print(f"[fullmkt] 全池语料 n={len(tr)} → {OUT} ({time.time()-t0:.0f}s)",
          flush=True)

    # ① 全池分板块画像
    tr["board"] = np.where(tr.symbol.str.startswith(("30", "68")), "双创", "主板")
    for b, g in tr.groupby("board"):
        print(f"  {b}: {len(g):,} 笔 均笔 {g.avg_pnl_pct.mean():+.2f}% "
              f"胜率 {(g.avg_pnl_pct > 0).mean():.1%}", flush=True)

    # ② 选序+守卫+评估(kt5/kt7)
    a60 = amihud_series(60)
    res = {}
    for kt in (5, 7):
        pool = prep(select_seq(a60, tr, kt, ascending=False))
        pool["board"] = np.where(pool.symbol.str.startswith(("30", "68")),
                                 "双创", "主板")
        skip = pool[~(pool.premium > 3.0)]
        t1 = skip[skip.cyb_ratio >= 1.0]
        for tag, sub in ((f"全市场纯|kt{kt}", skip), (f"全市场T1|kt{kt}", t1)):
            a, d, amin, amax = ev(sub, FULL)
            print(f"  {tag}: n={len(sub):,} ann {a:+.1f} dd {d:+.1f} "
                  f"cal {a/abs(d):.2f} 带[{amin:+.1f}~{amax:+.1f}]", flush=True)
            res[tag] = {"n": int(len(sub)), "ann": round(a, 2),
                        "dd": round(d, 2), "calmar": round(a / abs(d), 2)}
        # 选序构成+分板块
        vc = t1.board.value_counts(normalize=True).round(3).to_dict()
        print(f"    kt{kt} T1 臂构成: {vc}", flush=True)
        for b, g in t1.groupby("board"):
            print(f"      {b}: {len(g):,} 笔 均笔 {g.avg_pnl_pct.mean():+.2f}% "
                  f"胜率 {(g.avg_pnl_pct > 0).mean():.1%}", flush=True)
        ys = [ev(t1, y, 11)[0] for y in YEARS]
        print(f"    kt{kt} T1 分年: " + " ".join(f"{y:+5.1f}" for y in ys),
              flush=True)
        res[f"kt{kt}_years"] = [round(y, 1) for y in ys]
    json.dump(res, open("logs/committee_20260908/r6_fullmkt.json", "w",
                        encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[fullmkt] done {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
