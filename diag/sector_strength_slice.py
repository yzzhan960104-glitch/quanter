# -*- coding: utf-8 -*-
"""板块强度×颈线信号(2026-09-05 第四轮:用户假设=只买强势板块信号能否提高收益/胜率)。

板块定义:tushare industry(110 类,静态快照——历史重分类未跟踪,幸存者
警示注记)。行业等权日收益(全湖成分,不只池内)→行业指数→信号日 20/60 日
动量的横截面百分位=板块强度(0=最弱,1=最强,决策时可用无前视)。
闸纪律:inner 2025 定桶 → outer 2026 验证 + 分年稳定 → 组合口径。
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

sb = pd.read_parquet("data_lake/stock_basic.parquet")
ind_map = sb.set_index("ts_code")["industry"]

lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                       filters=[("date", ">=", pd.Timestamp("2020-06-01"))],
                       columns=["close"])
lake = lake[lake.index.get_level_values("symbol").isin(ind_map.index)]
ret = lake["close"].groupby(level="symbol").pct_change()
df = pd.DataFrame({"ret": ret})
df["industry"] = df.index.get_level_values("symbol").map(ind_map)
print(f"lake syms={df.index.get_level_values('symbol').nunique()} 行业覆盖 {df['industry'].notna().mean()*100:.1f}%", flush=True)

# 行业等权日收益 → 行业指数
sec_ret = df.groupby([pd.Grouper(freq="D", level="date"), "industry"])["ret"].mean()
sec_ret.index.names = ["date", "industry"]
sec_idx = (1 + sec_ret.unstack("industry").fillna(0)).cumprod()

for win in (20, 60):
    mom = sec_idx / sec_idx.shift(win) - 1.0
    rank = mom.rank(axis=1, pct=True)               # 横截面百分位(每日)
    rank.to_parquet(f"diag/sector_rank{win}.parquet")
print("sector ranks saved (20/60d)", flush=True)

# ── 合并到语料 ──
tr = pd.read_parquet("diag/neckline_regime_trades.parquet")
tr["industry"] = tr["symbol"].map(ind_map)
r20 = pd.read_parquet("diag/sector_rank20.parquet")
r60 = pd.read_parquet("diag/sector_rank60.parquet")
def lookup(rank_df, dates, inds):
    vals = []
    for d, i in zip(dates, inds):
        try:
            v = rank_df.at[d, i] if isinstance(rank_df.index, pd.DatetimeIndex) else np.nan
        except Exception:
            v = np.nan
        vals.append(v)
    return np.array(vals, dtype=float)
# rank 索引是日期(unstack 后 index=date)
tr["sec_r20"] = lookup(r20, tr["formed_at"], tr["industry"])
tr["sec_r60"] = lookup(r60, tr["formed_at"], tr["industry"])
tr = tr.dropna(subset=["sec_r20"])
tr.to_parquet("diag/sector_trades_tagged.parquet")
print(f"tagged n={len(tr)}", flush=True)

inner = tr[(tr.formed_at >= "2025-01-01") & (tr.formed_at < "2026-01-01")]
outer = tr[tr.formed_at >= "2026-01-01"]
print(f"inner n={len(inner)} 基线{inner.avg_pnl_pct.mean():+.2f}% | outer n={len(outer)} 基线{outer.avg_pnl_pct.mean():+.2f}%")

def q4(d, col="sec_r20"):
    e = [0, .25, .5, .75, 1.0001]
    return pd.cut(d[col], e, labels=["Q1最弱", "Q2", "Q3", "Q4最强"])

for name, d in (("inner", inner), ("outer", outer)):
    b = q4(d)
    print(f"\n== {name} 板块强度四分位(20日) ==")
    for lab in ["Q1最弱", "Q2", "Q3", "Q4最强"]:
        s = d[b == lab]
        print(f"  {lab}: n={len(s):>5}({len(s)/len(d)*100:.0f}%) 胜率{(s.avg_pnl_pct>0).mean()*100:>5.1f}% 均笔{s.avg_pnl_pct.mean():+6.2f}%")
    b6 = q4(d, "sec_r60")
    print(f"  [60日口径] " + " | ".join(
        f"{lab}:{d[b6==lab].avg_pnl_pct.mean():+.2f}%(n={len(d[b6==lab])})"
        for lab in ["Q1最弱", "Q2", "Q3", "Q4最强"]))
