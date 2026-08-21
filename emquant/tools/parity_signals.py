# -*- coding: utf-8 -*-
"""信号级对拍：同一识别内核（组装产物 §1）分别吃 data_lake 腿与 gm 腿数据，
逐标的比对 detect_signal 输出——直接预演 W2 双轨信号一致率（设计 §7 的真口径）。

物理意图（Why）：
    列级对拍（compare_data.py）量的是"数值同源性"，其 1e-6 容差会把两源的
    浮点/复权因子精度噪声（实测 1e-6~1e-5）也判为不一致——但 W2 的验收
    指标是**信号集合逐字段一致**，不是价格逐位一致。噪声是否传导到信号
    分叉，只能用本脚本直接量：两腿数据各跑一遍内核，比"有没有信号"与
    字段值（浮点字段带相对容差，默认 1e-4——比噪声地板高一个量级、比
    任何真实信号差异低四个量级）。

    运行环境：.venv310（需 pandas + data_lake 读栈；内核从组装产物加载，
    不依赖 gm——gm 腿数据是 parity_gm_*.csv 离线产物）。

用法：
    PYTHONUTF8=1 .venv310/Scripts/python.exe emquant/tools/parity_signals.py [--rel-tol 1e-4]
输出：
    汇总（presence 分叉数/字段超差数/一致率）+ 分叉明细 + 排除（窗口不足）
    落 emquant/state/parity_signals_YYYYMMDD.json
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "emquant" / "emquant_neckline_pilot.py"
GM_CSV = ROOT / f"emquant/state/parity_gm_{date.today():%Y%m%d}.csv"
LAKE = ROOT / "data_lake/a_shares_daily.parquet"
UNIVERSE = ROOT / "emquant/config/universe.json"
SNAP = ROOT / "emquant/config/params_snapshot.json"
REL_TOL = float(sys.argv[sys.argv.index("--rel-tol") + 1]) if "--rel-tol" in sys.argv else 1e-4
# 信号字段分组：浮点字段按相对容差比；其余（symbol/formed_at）精确等。
_FLOAT_KEYS = ("neckline", "bottom", "entry_price", "atr", "rr")


def _load_kernel():
    """加载组装产物（§1 内核 = 与仓库逐字节同源，C2 钉死）。"""
    spec = importlib.util.spec_from_file_location("pilot_for_parity", ARTIFACT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["pilot_for_parity"] = m  # PEP563 dataclass 回查需要先注册
    spec.loader.exec_module(m)
    return m


def main() -> int:
    kern = _load_kernel()
    snap = json.loads(SNAP.read_text(encoding="utf-8"))
    uni = json.loads(UNIVERSE.read_text(encoding="utf-8"))["symbols"]
    gm = pd.read_csv(GM_CSV)
    gm["date"] = pd.to_datetime(gm["date"])
    lake = pd.read_parquet(LAKE)

    rows = []
    n_both = n_none = n_presence_div = n_field_div = n_skipped = 0
    for sym in uni:
        # gm 腿：宽表截到该标的，DatetimeIndex
        g = gm[gm["symbol"] == sym].set_index("date").sort_index()
        try:
            l = lake.xs(sym, level="symbol").sort_index()
        except KeyError:
            n_skipped += 1
            continue
        if len(g) < 60 or len(l) < 60:
            n_skipped += 1
            continue
        # 两腿各自的识别日 = 各自末根（锚已对齐到同一天；防末根缺失的防御性 min）
        d_g, d_l = g.index[-1], l.index[-1]
        if d_g != d_l:
            # 末日不齐（停牌等）→ 截到公共末日，识别日用公共日
            d_common = min(d_g, d_l)
            g, l = g[g.index <= d_common], l[l.index <= d_common]
        detect_day = g.index[-1]
        try:
            sg = kern.detect_signal(sym, g, snap["id_params"], snap["exec_params"], detect_day)
            sl = kern.detect_signal(sym, l, snap["id_params"], snap["exec_params"], detect_day)
        except Exception as exc:  # 单标的异常不终止全量（对拍是观测不是交易）
            rows.append({"symbol": sym, "kind": "error", "detail": repr(exc)[:200]})
            n_skipped += 1
            continue
        if sg is None and sl is None:
            n_none += 1
            continue
        if (sg is None) != (sl is None):
            n_presence_div += 1
            side = "gm_only" if sl is None else "lake_only"
            rows.append({"symbol": sym, "kind": f"presence_{side}",
                         "gm": None if sg is None else {k: getattr(sg, k) for k in _FLOAT_KEYS},
                         "lake": None if sl is None else {k: getattr(sl, k) for k in _FLOAT_KEYS}})
            continue
        n_both += 1
        bad = []
        for k in _FLOAT_KEYS:
            a, b = getattr(sg, k), getattr(sl, k)
            if a is None or b is None:
                if a is not b:
                    bad.append(k)
                continue
            if abs(a - b) > REL_TOL * max(abs(a), abs(b), 1e-12):
                bad.append(k)
        if bad:
            n_field_div += 1
            rows.append({"symbol": sym, "kind": f"field_{'+'.join(bad)}",
                         "gm": {k: getattr(sg, k) for k in _FLOAT_KEYS},
                         "lake": {k: getattr(sl, k) for k in _FLOAT_KEYS}})
    total = n_both + n_none + n_presence_div + n_field_div
    out = {
        "date": f"{date.today():%Y-%m-%d}", "rel_tol": REL_TOL, "total_compared": total,
        "signal_both": n_both, "signal_none": n_none,
        "presence_divergence": n_presence_div, "field_divergence": n_field_div,
        "skipped": n_skipped, "details": rows,
    }
    p = ROOT / f"emquant/state/parity_signals_{date.today():%Y%m%d}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[parity_signals] 识别日锚={detect_day} 容差={REL_TOL:g}")
    print(f"[parity_signals] 可比 {total} | 双侧有信号 {n_both} | 双侧无信号 {n_none} | "
          f"presence 分叉 {n_presence_div} | 字段超差 {n_field_div} | 跳过 {n_skipped}")
    print(f"[parity_signals] 信号级一致率（presence+字段全同）= "
          f"{(n_none + n_both - n_field_div) / total * 100:.2f}%" if total else "无可比标的")
    print(f"[parity_signals] 明细：{p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
