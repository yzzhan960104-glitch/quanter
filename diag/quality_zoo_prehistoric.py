# -*- coding: utf-8 -*-
"""因子家族淘汰赛 · Phase 1：史前窗 2010-2020 方向复现（2026-08-29 · 方案定稿）。

预登记（先于运行写死，全文见 docs/superpowers/plans/
2026-08-29-factor-family-tournament.md Phase 1）：
  - 全市场（含退市）B3 内核原参数重扫 2010-01-01~2020-12-31（universe=该日
    有数据，绕开今日截面；warmup 数据自 2008-06 起加载）；
  - 参测代表（12 列，方向自动取自 registry.jsonl 主窗存活记录）：
      吞吐轴：suppression / neck_rel / risk_pct_v / alpha60 / alpha120(参照)
              / kaufman_er60
      质量轴：pk_vol60 / gk_vol60 / tail_spread60 / idio_vol60 / amihud20
              / amihud60
  - 裁决（每特征三条件，全过=时间外存活）：
      ① 总体 hi30−lo30 与主窗方向同号；
      ② 逐年(2010-2020, 11 年)同向 ≥8/11（样本 <200 侧计异向）；
      ③ 史前窗截面 IC（日内去均值秩相关）与主窗方向同号；
    ①∧③ 同号但 ② <8/11 → regime 依赖降级观察；① 反号 → 杀。
  - 判定单位=特征方向，非组合 ann。

用法：PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_zoo_prehistoric.py
产物：logs/quality/factor_zoo/{prehistoric_trades.parquet,
prehistoric_verdict.json, prehistoric_verdict.md}
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from diag.quality_momentum_batch2 import _lk, _matrix
from diag.quality_p0_features import _scan_pool

REG = "logs/quality/factor_zoo/registry.jsonl"
OUT_DIR = "logs/quality/factor_zoo"
STATE_PATH = "logs/r6_10_loop/state.json"
LOAD_FROM = pd.Timestamp("2008-06-01")
DATE_END = pd.Timestamp("2020-12-31")
V_START = pd.Timestamp("2010-01-01")
YEARS = list(range(2010, 2021))
FEATURES = {
    "avg_pnl_pct": ["pk_vol60", "gk_vol60", "tail_spread60", "idio_vol60",
                    "amihud20", "amihud60"],
    "occ": ["suppression", "neck_rel", "risk_pct_v", "alpha60", "alpha120",
            "kaufman_er60"],
}
MIN_YEAR_SIDE = 200
T0 = time.time()


def _log(m):
    print(f"[{time.time() - T0:>6.0f}s] {m}", flush=True)


def _wic(df, col, target):
    v = df[col].to_numpy(dtype=float)
    y = df[target].to_numpy(dtype=float)
    ok = np.isfinite(v) & np.isfinite(y)
    if ok.sum() < 200:
        return None
    g = pd.factorize(df["signal_date"].to_numpy()[ok])[0]
    xr = pd.Series(v[ok]).groupby(g).rank(pct=True).to_numpy()
    yr = pd.Series(y[ok]).groupby(g).rank(pct=True).to_numpy()
    z = pd.DataFrame({"g": g, "x": xr, "y": yr}).groupby("g")[["x", "y"]] \
        .transform(lambda s: (s - s.mean()) /
                   (s.std(ddof=0) if s.std(ddof=0) > 0 else 1.0))
    return float(np.corrcoef(z["x"], z["y"])[0, 1])


def _contrast(df, col, target):
    s = df[col].dropna()
    if len(s) < 200:
        return None
    lo_t, hi_t = s.quantile([0.3, 0.7])
    lo = df.loc[df[col] <= lo_t, target]
    hi = df.loc[df[col] >= hi_t, target]
    if len(lo) < 20 or len(hi) < 20:
        return None
    return float(hi.mean() - lo.mean())


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    # 主窗方向与 wic 从登记册自动取用（防转录错误）
    reg = [json.loads(l) for l in open(REG, encoding="utf-8")]
    dir_of = {}
    for target, feats in FEATURES.items():
        for f in feats:
            r = next((x for x in reg if x["feature"] == f
                      and x["target"] == target and x.get("survivor")), None)
            if r is None:
                raise RuntimeError(f"{f}×{target} 不在主窗存活集，参测名单有误")
            dir_of[(f, target)] = r["direction"]
    _log(f"12 代表方向载入：{ {f'{k[0]}×{k[1][:4]}': v for k, v in dir_of.items()} }")

    # —— 数据：全湖 2008-06~2020-12 OHLCV（含退市）——
    _log(f"load lake {LOAD_FROM.date()}~{DATE_END.date()}")
    tb = pq.read_table("data_lake/a_shares_daily.parquet",
                       columns=["date", "symbol", "open", "high", "low",
                                "close", "volume", "amount"],
                       filters=[("date", ">=", LOAD_FROM.to_pydatetime()),
                                ("date", "<=", DATE_END.to_pydatetime())])
    d = pd.to_datetime(tb.column("date").to_pandas())
    s = tb.column("symbol").to_pandas().astype(str)
    long_df = pd.DataFrame({
        "open": tb.column("open").to_pandas().to_numpy(),
        "high": tb.column("high").to_pandas().to_numpy(),
        "low": tb.column("low").to_pandas().to_numpy(),
        "close": tb.column("close").to_pandas().to_numpy(),
        "volume": tb.column("volume").to_pandas().to_numpy(),
        "amount": tb.column("amount").to_pandas().to_numpy(),
    }, index=pd.MultiIndex.from_arrays([d, s], names=["date", "symbol"]))
    del tb
    universe = {sym: sub.droplevel("symbol").sort_index()
                for sym, sub in long_df.groupby(level="symbol")}
    _log(f"universe {len(universe)} 符号（含退市）")

    # —— B3 原参数重扫 ——
    base = json.load(open(STATE_PATH, encoding="utf-8"))["base"]
    ph_df, n_fail = _scan_pool("prehistoric", base, universe)
    del universe, long_df
    ph_df["signal_date"] = pd.to_datetime(ph_df["signal_date"])
    ph = ph_df[ph_df["signal_date"] >= V_START].reset_index(drop=True).copy()
    ph["year"] = ph["signal_date"].dt.year
    ph["occ"] = pd.to_numeric(ph["wait_days"], errors="coerce") + \
        pd.to_numeric(ph["holding_bars"], errors="coerce")
    ph["neck_rel"] = ph["neckline"] / ph["entry"]
    ph["risk_pct_v"] = pd.to_numeric(ph["risk_pct"], errors="coerce")
    _log(f"史前窗流水 {len(ph)} 笔（{ph['year'].min()}~{ph['year'].max()}，"
         f"年分布 {ph.groupby('year').size().to_dict()}）")
    ph.to_parquet(os.path.join(OUT_DIR, "prehistoric_trades.parquet"),
                  index=False)

    # —— 特征宽表（价格族）——
    _log("compute price features on prehistoric window")
    close = ph_close = None
    wide = {}
    tb2 = pq.read_table("data_lake/a_shares_daily.parquet",
                        columns=["date", "symbol", "open", "high", "low",
                                 "close", "amount"],
                        filters=[("date", ">=", LOAD_FROM.to_pydatetime()),
                                 ("date", "<=", DATE_END.to_pydatetime())])
    d2 = pd.to_datetime(tb2.column("date").to_pandas())
    s2 = tb2.column("symbol").to_pandas().astype(str)
    dc, du = pd.factorize(d2)
    sc, su = pd.factorize(s2)
    mat = {}
    for c in ("open", "high", "low", "close", "amount"):
        m = np.full((len(du), len(su)), np.nan, dtype="float32")
        m[dc, sc] = tb2.column(c).to_pandas().to_numpy(dtype="float32")
        mat[c] = pd.DataFrame(m, index=pd.DatetimeIndex(du), columns=list(su))
    del tb2
    close, high, low, open_, amount = (mat["close"], mat["high"], mat["low"],
                                       mat["open"], mat["amount"])
    ret = close.pct_change(fill_method=None).astype("float32")
    mkt = ret.mean(axis=1)
    log_hl = np.log(high / low).astype("float32") ** 2
    log_co = np.log(close / open_).astype("float32") ** 2
    wide["pk_vol60"] = np.sqrt(log_hl.rolling(60, min_periods=48).mean()
                               / (4 * np.log(2)))
    wide["gk_vol60"] = np.sqrt((0.5 * log_hl - (2 * np.log(2) - 1) * log_co)
                               .rolling(60, min_periods=48).mean())
    wide["tail_spread60"] = (ret.rolling(60, min_periods=48).quantile(0.95)
                             - ret.rolling(60, min_periods=48).quantile(0.05))
    # idio_vol60（行业残差波动；stock_basic 行业含退市股）
    sb = pq.read_table("data_lake/stock_basic.parquet",
                       columns=["ts_code", "industry"]).to_pandas()
    ind_map = dict(zip(sb["ts_code"].astype(str), sb["industry"].astype(str)))
    ind_wide = pd.DataFrame(np.nan, index=close.index, columns=close.columns,
                            dtype="float32")
    ind_groups = {}
    for sym in close.columns:
        ind_groups.setdefault(ind_map.get(sym, np.nan), []).append(sym)
    for name, cols in ind_groups.items():
        if name is None or name != name:
            continue
        ser = ret[cols].mean(axis=1).astype("float32")
        ind_wide[cols] = np.repeat(ser.to_numpy()[:, None], len(cols), axis=1)
    wide["idio_vol60"] = (ret - ind_wide).rolling(60, min_periods=48).std()
    del ind_wide
    amihud_base = (ret.abs() / (amount + 1.0)).astype("float32")
    wide["amihud20"] = amihud_base.rolling(20, min_periods=16).mean()
    wide["amihud60"] = amihud_base.rolling(60, min_periods=48).mean()
    diff = close.diff().abs()
    wide["kaufman_er60"] = ((close - close.shift(60)).abs()
                            / (diff.rolling(60, min_periods=48).sum() + 1e-12)
                            ).astype("float32")
    xm = ret.mul(mkt, axis=0).rolling(250, min_periods=200).mean()
    rm_ = ret.rolling(250, min_periods=200).mean()
    mm_ = mkt.rolling(250, min_periods=200).mean()
    mv_ = mkt.rolling(250, min_periods=200).var()
    beta = xm.sub(rm_.mul(mm_, axis=0)).div(mv_, axis=0)
    for w in (60, 120):
        wide[f"alpha{w}"] = (ret.rolling(w, min_periods=int(w * .8)).mean()
                             - beta.mul(mkt.rolling(w, min_periods=int(w * .8))
                                        .mean(), axis=0)).astype("float32")
    del xm, rm_, mm_, mv_, beta, log_hl, log_co, amihud_base, diff, mat

    td, ts = ph["signal_date"], ph["symbol"]
    for k, w in wide.items():
        ph[k] = _lk(w.astype("float32"), td, ts)
    _log("features attached")

    # —— 裁决 ——
    out = {}
    for target, feats in FEATURES.items():
        for f in feats:
            d_main = dir_of[(f, target)]
            c_all = _contrast(ph, f, target)
            yr = {}
            same = 0
            for y in YEARS:
                cy = _contrast(ph[ph["year"] == y], f, target)
                yr[y] = None if cy is None else round(cy, 2)
                if cy is not None and np.sign(cy) == d_main:
                    same += 1
            w_ = _wic(ph, f, target)
            w_same = w_ is not None and w_ == w_ and np.sign(w_) == d_main
            c_same = c_all is not None and np.sign(c_all) == d_main
            if c_same and w_same and same >= 8:
                verdict = "时间外存活"
            elif c_same and w_same:
                verdict = f"regime依赖降级观察({same}/11)"
            else:
                verdict = "杀"
            out[f] = {"axis": target, "direction": d_main,
                      "contrast": None if c_all is None else round(c_all, 3),
                      "years_same": same, "year_detail": yr,
                      "wic": None if w_ is None or w_ != w_ else round(w_, 4),
                      "verdict": verdict}
            _log(f"[verdict] {f}×{target}: Δ={c_all} {same}/11 年 "
                 f"wic={w_} → {verdict}")

    with open(os.path.join(OUT_DIR, "prehistoric_verdict.json"), "w",
              encoding="utf-8") as f:
        json.dump({"rule": "总体同向 ∧ ≥8/11 年 ∧ wic 同向；两同向但年<8=降级",
                   "verdicts": out,
                   "n_trades": len(ph),
                   "scan_fail_symbols": n_fail,
                   "generated_at": pd.Timestamp.now().isoformat()},
                  f, ensure_ascii=False, indent=1, default=str)
    L = ["# Phase 1 · 史前窗 2010-2020 方向复现（2026-08-29）", "",
         f"> 流水 {len(ph)} 笔 / 扫描失败符号 {n_fail}；规则：总体同向 ∧ "
         "≥8/11 年 ∧ 截面IC 同向。", "",
         "| 特征 | 轴 | 方向 | 总对比 | 年同向 | 截面IC | 裁决 |",
         "|---|---|---|---|---|---|---|"]
    for f, v in out.items():
        L.append(f"| {f} | {v['axis']} | {v['direction']:+d} | "
                 f"{v['contrast']} | {v['years_same']}/11 | {v['wic']} | "
                 f"**{v['verdict']}** |")
    with open(os.path.join(OUT_DIR, "prehistoric_verdict.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(L))
    _log(f"[done] {OUT_DIR}/prehistoric_verdict.md 总 "
         f"{(time.time() - T0) / 60:.0f}min")


if __name__ == "__main__":
    main()
