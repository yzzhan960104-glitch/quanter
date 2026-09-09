# -*- coding: utf-8 -*-
"""R5 选序层网格(keep-top 族实盘口径首扫,2026-09-08 会战续)。

未开拓面:08-29 裁决「最不流动=质量侧」定型 keep-top5+amihud60 后,选序层
三要素(N/窗口/选序键)从未系统扫描。全部 DataFrame 层,掘金 AMIHUD_FILTER
有 keep_top 部署键。判据预登记(先于看数):
  采纳需:全期 calmar > 基线 10% 相对提升 ∧ dd 不劣于基线+1pp ∧ 六年无新负年
  ∧ inner/outer 双窗同向(复审硬门槛 ts8 教训)。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd

from diag.opt0908_lib import CAL, CYB, _LAKE, ev, report_arms
from discovery.split import Segment

FULL = Segment("f", pd.Timestamp("2021-01-01").date(),
               pd.Timestamp("2026-09-04").date())
INNER = Segment("i", pd.Timestamp("2021-01-01").date(),
                pd.Timestamp("2024-12-31").date())
OUTER = Segment("o", pd.Timestamp("2025-01-01").date(),
                pd.Timestamp("2026-09-04").date())

_SRC = pd.read_parquet("diag/retrial_corpus_rolling.parquet")
_SRC["formed_at"] = pd.to_datetime(_SRC["formed_at"])
_SYMS = set(_SRC["symbol"].unique())
_LK = _LAKE[_LAKE.index.get_level_values("symbol").isin(_SYMS)]
_RET = _LK["close"].groupby(level="symbol").pct_change()
_AMT = _LK["amount"].astype("float32")


def amihud_series(window: int) -> pd.Series:
    am = (_RET.abs() / (_AMT + 1.0)).groupby(level="symbol") \
        .rolling(window, min_periods=max(8, window // 2)).mean()
    return am.reset_index(level=0, drop=True).sort_index()


def select_seq(metric: pd.Series, sub: pd.DataFrame, n: int,
               ascending: bool) -> pd.DataFrame:
    """按 metric 选每日前 n(ascending=True=值最小的 n 只优先)。"""
    vals = metric.reindex(pd.MultiIndex.from_arrays(
        [sub["formed_at"], sub["symbol"]])).values
    work = sub.assign(_m=vals)

    def top(g: pd.DataFrame) -> pd.Series:
        m = pd.Series(True, index=g.index)
        if len(g) <= n:
            return m
        v = g.dropna(subset=["_m"])
        if len(v) < n:
            return m
        k = v.sort_values("_m", ascending=ascending).head(n).index
        m.loc[g.index.difference(k)] = False
        return m

    sel = work.groupby("formed_at", group_keys=False).apply(top)
    return sub[sel]


def build_t1(sub: pd.DataFrame) -> pd.DataFrame:
    s = sub[~(sub["premium"] > 3.0)]
    return s[s["cyb_ratio"] >= 1.0]


def prep(sub: pd.DataFrame) -> pd.DataFrame:
    out = sub.copy()
    out["premium"] = ((out["entry_price"] - out["neckline"]) / out["atr"])
    out["cyb_ratio"] = (CYB / CYB.rolling(60).mean()) \
        .reindex(out["formed_at"]).values
    return out


def main() -> None:
    arms: dict[str, pd.DataFrame] = {}
    # 基线:amihud60 keep-top5(最不流动优先=降序)
    a60 = amihud_series(60)
    base = prep(select_seq(a60, _SRC, 5, ascending=False))
    arms["基线 kt5/amihud60/最不流动"] = base

    # 轴 1:keep_top N
    for n in (3, 4, 6, 8):
        arms[f"kt{n}/amihud60"] = prep(select_seq(a60, _SRC, n, ascending=False))

    # 轴 2:amihud 窗口
    for w in (20, 40, 120):
        am = amihud_series(w)
        arms[f"kt5/amihud{w}"] = prep(select_seq(am, _SRC, 5, ascending=False))

    # 轴 3:选序键变体(同 N=5)
    amt = _AMT.groupby(level="symbol").rolling(60, min_periods=30).mean()
    amt = amt.reset_index(level=0, drop=True).sort_index()
    arms["kt5/成交额最小优先"] = prep(select_seq(amt, _SRC, 5, ascending=True))
    arms["kt5/最流动优先(反向对照)"] = prep(select_seq(a60, _SRC, 5, ascending=True))

    print("===== 全信号池(纯收益栈/均衡栈 T1 双臂) =====", flush=True)
    res = {}
    for name, pool in arms.items():
        skip = pool[~(pool["premium"] > 3.0)]
        t1 = skip[skip["cyb_ratio"] >= 1.0]
        for tag, sub in ((f"{name}|纯", skip), (f"{name}|T1", t1)):
            a, d, amin, amax = ev(sub, FULL)
            ai = ev(sub, INNER, 11)[0]
            ao = ev(sub, OUTER, 11)[0]
            cal = a / abs(d) if d else float("nan")
            print(f"{tag:<28}{len(sub):>6}{a:>+8.1f}%{d:>+7.1f}% cal {cal:>5.2f}"
                  f" | inner {ai:>+6.1f} outer {ao:>+6.1f}", flush=True)
            res[tag] = {"n": int(len(sub)), "ann": round(a, 2), "dd": round(d, 2),
                        "calmar": round(cal, 2), "inner": round(ai, 2),
                        "outer": round(ao, 2)}
    json.dump(res, open("logs/committee_20260908/r5_seqgrid.json", "w",
                        encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
