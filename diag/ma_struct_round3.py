# -*- coding: utf-8 -*-
"""MA 结构轮(第三轮 · 2026-09-05):多线之间的结构 × 信号关系。

用户定维:不看单线,看多线结构(排列/间距/收敛发散)与信号(颈线/突破)的相对关系。
预注册结构特征(inner 定桶 → outer 验证,双窗同向才进组合口径;多重检验警示:
本语料已第三轮,幸存条件必须过组合闸才能当结论):
  A 排列结构 ord_score = (20>50)+(50>120)+(120>200),0..3
  B 信号位置:颈线在均线带的上方/带内上段/带内下段/下方(相对四线 min-max)
  C 带宽动态:width 10 日变化(收敛/发散),由 slp 反推 T-10 的 MA 值
  D 带内形状:相邻线间距向量 (g1,g2,g3)/ATR → 结构原型
  E 四个结构原型(经济假设):
    P1 粘合突破: 宽度低分位 + 收敛 + 颈线带上
    P2 趋势回调再突破: ma120>ma200 且 ma200 斜率>0, 但 g1=(20-50)/ATR 压缩(<1), 颈线带上
    P3 底部反转: ord_score<=1 + 宽度低分位 + 颈线带上(新趋势出生)
    P4 末端延伸: 三间距全正 + 宽度高分位(老趋势晚期)
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

tr = pd.read_parquet("diag/ma3d_trades_tagged.parquet").dropna(subset=["slp200"])
M = tr[["ma20", "ma50", "ma120", "ma200"]].values

# A 排列序
tr["ord_score"] = ((tr.ma20 > tr.ma50).astype(int) + (tr.ma50 > tr.ma120).astype(int)
                   + (tr.ma120 > tr.ma200).astype(int))
# B 信号(颈线)在带中的位置
rmin, rmax = M.min(1), M.max(1)
span = np.clip(rmax - rmin, 1e-9, None)
tr["neck_pos"] = (tr["neckline"] - rmin) / span            # <0 带下,0-1 带内,>1 带上
# C 带宽动态(由 slp 反推 T-10: ma_prev = ma/(1+slp))
Mp = tr[["ma20", "ma50", "ma120", "ma200"]].values / (
    1.0 + tr[["slp20", "slp50", "slp120", "slp200"]].values)
w_now = (M.max(1) - M.min(1)) / tr["close"].values
w_prev = (Mp.max(1) - Mp.min(1)) / tr["close"].values
tr["width_conv"] = w_now / np.clip(w_prev, 1e-9, None) - 1.0   # <0 收敛,>0 发散
# D 带内形状
tr["g1"] = (tr.ma20 - tr.ma50) / tr["atr"]
tr["g2"] = (tr.ma50 - tr.ma120) / tr["atr"]
tr["g3"] = (tr.ma120 - tr.ma200) / tr["atr"]

inner = tr[tr.seg == "inner"]; outer = tr[tr.seg == "outer"]
wq25, wq75 = inner.width_ribbon.quantile([.25, .75])
print(f"inner n={len(inner)} 基线{inner.avg_pnl_pct.mean():+.2f}% | outer n={len(outer)} 基线{outer.avg_pnl_pct.mean():+.2f}%")

def cmp2(label, mask):
    """双窗同向快报:两窗内 通过组均笔-基线 同号才算稳定。"""
    res = []
    for d in (inner, outer):
        a, b = d[mask.loc[d.index]], d[~mask.loc[d.index]]
        res.append((len(a), a.avg_pnl_pct.mean() - d.avg_pnl_pct.mean(),
                    len(b), b.avg_pnl_pct.mean() - d.avg_pnl_pct.mean()))
    (na, da, nb, db), (nc, dc, nd, dd) = res
    stable = "稳定✓" if da * dc > 0 else "翻转✗"
    print(f"{label:<30} 内通过 n={na:>5} Δ{da:+5.2f}pp(被弃Δ{db:+5.2f}) "
          f"｜ 外通过 n={nc:>5} Δ{dc:+5.2f}pp(被弃Δ{dd:+5.2f})  {stable}")

print("\n── A 排列结构 ──")
for s in range(4):
    m = (tr.ord_score == s); cmp2(f"ord_score={s}", m)
print("\n── B 颈线位置 ──")
for lab, m in [("带下(<0)", tr.neck_pos < 0), ("带内下段(0-.5)", (tr.neck_pos >= 0) & (tr.neck_pos < .5)),
               ("带内上段(.5-1)", (tr.neck_pos >= .5) & (tr.neck_pos <= 1)), ("带上(>1)", tr.neck_pos > 1)]:
    cmp2(f"颈线{lab}", m)
print("\n── C 带宽动态(收敛/发散) ──")
cq = inner.width_conv.quantile([1/3, 2/3]).values
for lab, m in [("收敛(<%+.1f%%)" % (cq[0]*100), tr.width_conv < cq[0]),
               ("平稳", (tr.width_conv >= cq[0]) & (tr.width_conv <= cq[1])),
               ("发散(>%+.1f%%)" % (cq[1]*100), tr.width_conv > cq[1])]:
    cmp2(f"带宽10日{lab}", m)
print("\n── D 带内形状 ──")
for lab, m in [("g1 压缩(|g1|<1)", tr.g1.abs() < 1), ("g2 压缩(|g2|<1)", tr.g2.abs() < 1),
               ("三距全正", (tr.g1 > 0) & (tr.g2 > 0) & (tr.g3 > 0)),
               ("三距全负", (tr.g1 < 0) & (tr.g2 < 0) & (tr.g3 < 0))]:
    cmp2(lab, m)
print("\n── E 结构原型(组合) ──")
cmp2("P1 粘合突破(窄+收敛+带上)",
     (tr.width_ribbon <= wq25) & (tr.width_conv < cq[0]) & (tr.neck_pos > 1))
cmp2("P2 趋势回调再突破",
     (tr.ma120 > tr.ma200) & (tr.slp200 > 0) & (tr.g1 < 1) & (tr.neck_pos > 1))
cmp2("P3 底部反转(ord≤1+窄+带上)",
     (tr.ord_score <= 1) & (tr.width_ribbon <= wq25) & (tr.neck_pos > 1))
cmp2("P4 末端延伸(全正+宽)",
     (tr.g1 > 0) & (tr.g2 > 0) & (tr.g3 > 0) & (tr.width_ribbon >= wq75))
tr.to_parquet("diag/ma_struct_trades_tagged.parquet")
print("\nsaved diag/ma_struct_trades_tagged.parquet")
