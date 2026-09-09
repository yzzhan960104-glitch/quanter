# -*- coding: utf-8 -*-
"""MA 三维度(斜率/宽度/支撑)系统重测 · 2026-09-05 第二轮。

用户定维:MA 不看交叉,看 ①斜率(MA 方向强度,分级) ②宽度(均线带粘合/发散)
③支撑(颈线/形态与 MA 的相对位置,ATR 归一)。
纪律:inner 2025 定分桶/阈值 → outer 2026 同桶验证;双窗稳定才进组合口径。
全部 formed_at 快照,无前视。语料复用 diag/ma_gate_trades_tagged.parquet。
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd

MAS = (20, 50, 120, 200)
tr = pd.read_parquet("diag/ma_gate_trades_tagged.parquet").dropna(subset=["ma200"])

# ── 补算斜率数值(需要 MA 在 T-10 的值,湖重算后按 (symbol, formed_at) 合并) ──
syms = tr["symbol"].unique()
lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2023-09-01"))],
                       columns=["close"])
lake = lake[lake.index.get_level_values("symbol").isin(syms)]
rows = []
for sym, g in lake.groupby(level="symbol", sort=False):
    g = g.droplevel("symbol").sort_index()
    c = g["close"]
    d = {"symbol": sym, "date": g.index}
    for m in MAS:
        ma = c.rolling(m, min_periods=m).mean()
        d[f"slp{m}"] = (ma / ma.shift(10) - 1.0).values      # 10日百分比斜率
    rows.append(pd.DataFrame(d).dropna(subset=[f"slp{m}" for m in MAS]))
slp = pd.concat(rows)
slp["date"] = pd.to_datetime(slp["date"])
tr = tr.drop(columns=[c for c in tr.columns if c.startswith("slp")], errors="ignore")
tr = tr.merge(slp, left_on=["symbol", "formed_at"], right_on=["symbol", "date"],
              how="left", suffixes=("", "_slp"))
tr = tr.dropna(subset=["slp200"])

# ── 三维度列 ──
mascols = tr[[f"ma{m}" for m in MAS]].values
tr["width_ribbon"] = (mascols.max(1) - mascols.min(1)) / tr["close"]      # 均线带宽度
tr["width_fasts"] = (tr["ma20"] - tr["ma200"]) / tr["ma200"]              # 快慢价差(带符号)
# 支撑:颈线下方最近的 MA,距离以 ATR 计;无下方 MA = NaN
below = np.where(mascols < tr["neckline"].values[:, None], mascols, np.nan)
nearest = np.nanmax(below, axis=1)
tr["sup_depth"] = (tr["neckline"] - nearest) / tr["atr"]                  # 颈线离下方支撑的 ATR 数
tr["sup200"] = (tr["neckline"] - tr["ma200"]) / tr["atr"]                 # 颈线在 MA200 上方的 ATR 数
tr["bottom_above200"] = tr["bottom"] > tr["ma200"]

inner = tr[tr["seg"] == "inner"]
outer = tr[tr["seg"] == "outer"]
print(f"inner n={len(inner)} 基线均笔{inner.avg_pnl_pct.mean():+.2f}% | "
      f"outer n={len(outer)} 基线均笔{outer.avg_pnl_pct.mean():+.2f}%\n")

def bucket_test(col, edges, labels, name):
    """inner 分桶统计 → 同 edges 套 outer。双窗同向单调才报 STABLE。"""
    bi = pd.cut(inner[col], edges, labels=labels)
    bo = pd.cut(outer[col], edges, labels=labels)
    out = []
    for lab in labels:
        i, o = inner[bi == lab], outer[bo == lab]
        if len(i) < 60 or len(o) < 60:
            out.append(f"  {lab:<14} n内{len(i):>5}/外{len(o):>5} (样本不足)")
            continue
        out.append(f"  {lab:<14} 内 n={len(i):>5} 胜{(i.avg_pnl_pct>0).mean()*100:>4.1f}% 均{i.avg_pnl_pct.mean():+6.2f}%"
                   f" ｜ 外 n={len(o):>5} 胜{(o.avg_pnl_pct>0).mean()*100:>4.1f}% 均{o.avg_pnl_pct.mean():+6.2f}%")
    print(f"[{name}]\n" + "\n".join(out) + "\n", flush=True)

# inner 分位定桶(防手挑阈值),套 outer
def qedges(col, n=4):
    q = inner[col].quantile([i / n for i in range(1, n)]).values
    return np.r_[-1e9, q, 1e9], [f"Q{k+1}" for k in range(n)]

for m in MAS:
    e, l = qedges(f"slp{m}")
    bucket_test(f"slp{m}", e, [f"slp{m}-{x}" for x in l], f"斜率: MA{m} 10日变化分位")
e, l = qedges("width_ribbon")
bucket_test("width_ribbon", e, [f"rib-{x}" for x in l], "宽度: 四线带幅/close 分位")
bucket_test("width_fasts", [-1e9, -0.05, -0.015, 0.015, 0.05, 1e9],
            ["空头发散<-5%", "空头带-5~-1.5%", "粘合±1.5%", "多头带1.5~5%", "多头发散>5%"],
            "宽度: (MA20-MA200)/MA200")
bucket_test("sup_depth", [-1e9, 1, 2, 4, 1e9],
            ["贴支撑<1ATR", "1~2ATR", "2~4ATR", ">4ATR/无"], "支撑: 颈线离下方最近MA(ATR)")
bucket_test("sup200", [-1e9, 0, 2, 5, 1e9],
            ["MA200上方", "0~2ATR", "2~5ATR", ">5ATR"], "支撑: 颈线在MA200上方(ATR)")
a, b = inner[inner.bottom_above200], inner[~inner.bottom_above200]
c, d = outer[outer.bottom_above200], outer[~outer.bottom_above200]
print(f"[支撑: 形态底>MA200] 内 ✓n={len(a)} 均{a.avg_pnl_pct.mean():+.2f}% ✗n={len(b)} 均{b.avg_pnl_pct.mean():+.2f}%"
      f" ｜ 外 ✓n={len(c)} 均{c.avg_pnl_pct.mean():+.2f}% ✗n={len(d)} 均{d.avg_pnl_pct.mean():+.2f}%")
tr.to_parquet("diag/ma3d_trades_tagged.parquet")
print("\nsaved diag/ma3d_trades_tagged.parquet")
