# -*- coding: utf-8 -*-
"""湖数据 vs 掘金实跑信号对拍（2026-08-31 · 回答"预演 2 笔 vs 实跑 5 笔"根因）。

方法：importlib 加载掘金产物（2987dc20，与终端部署同版），用它自带的
detect_signal + ID_PARAMS/EXEC_PARAMS（R6-8 快照），喂【湖】截至 2026-08-28 的
日线（前复权口径以湖为准），对比 09:31 实跑 audit 里的掘金口径值。
零写入、零 SDK、零 state。
"""
import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PILOT = ROOT / "emquant" / "emquant_neckline_pilot.py"

# 产物加载（dataclass PEP563：先注册 sys.modules 再 exec）
spec = importlib.util.spec_from_file_location("pilot_artifact", PILOT)
m = importlib.util.module_from_spec(spec)
sys.modules["pilot_artifact"] = m
spec.loader.exec_module(m)
print(f"[artifact] stamp={m.PILOT_BUILD_STAMP}")

T_MINUS_1 = "2026-08-28"
SYMS = ["300803.SZ", "300747.SZ", "301123.SZ", "300593.SZ", "300655.SZ", "300456.SZ"]
# 09:31 实跑 audit（掘金口径：定点前复权钉 08-28）
GM = {
    "300803.SZ": dict(neckline=84.93, atr=4.4343, entry=96.0158, rr=2.801),
    "300747.SZ": dict(neckline=35.27, atr=3.0025, entry=42.7763, rr=2.012),
    "301123.SZ": dict(neckline=75.00, atr=8.9360, entry=97.3400, rr=2.431),
    "300593.SZ": dict(neckline=28.50, atr=2.5218, entry=34.8046, rr=3.545),
    "300655.SZ": dict(neckline=13.10, atr=1.1127, entry=15.8817, rr=2.193),
    "300456.SZ": dict(skip="held"),
}

lake = pd.read_parquet(ROOT / "data_lake" / "a_shares_daily.parquet")
n_roots = 2 * int(m.ID_PARAMS["window"]) + 40

print(f"\n{'symbol':<11} {'湖信号':<6} {'湖neckline':>10} {'gm neck':>8} {'湖atr':>8} {'gm atr':>8} {'湖rr':>7} {'gm rr':>7} {'湖formed':>11}")
for sym in SYMS:
    sub = lake.xs(sym, level="symbol").sort_index()
    df = sub.loc[:T_MINUS_1, ["open", "high", "low", "close", "volume"]].tail(n_roots).copy()
    df.index = pd.DatetimeIndex(pd.to_datetime(df.index)).normalize()
    try:
        sig = m.detect_signal(sym, df, m.ID_PARAMS, m.EXEC_PARAMS, T_MINUS_1)
    except Exception as e:
        print(f"{sym:<11} EXC {type(e).__name__}: {e}")
        continue
    g = GM[sym]
    if sig is None:
        print(f"{sym:<11} {'None':<6} {'—':>10} {g.get('neckline','—'):>8} {'—':>8} {g.get('atr','—'):>8} {'—':>7} {g.get('rr','—'):>7}  (gm skip={g.get('skip','-')})")
    else:
        formed = pd.Timestamp(sig.formed_at).strftime("%Y-%m-%d") if sig.formed_at is not None else "?"
        print(f"{sym:<11} {'SIGNAL':<6} {sig.neckline:>10.2f} {g.get('neckline','—'):>8} {sig.atr:>8.3f} {g.get('atr','—'):>8} {sig.rr:>7.3f} {g.get('rr','—'):>7} {formed:>11}")

print("\n[gate] min_rr =", m.ID_PARAMS.get("min_rr"), "| window =", m.ID_PARAMS.get("window"))
