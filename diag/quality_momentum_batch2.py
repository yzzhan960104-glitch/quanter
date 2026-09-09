# -*- coding: utf-8 -*-
"""信号质量 · 文献动量因子 batch2（2026-08-28 · 12 月窗口精确参数版）。

用户追加 8 方向（上轮 20 因子的文献精确参数化）。与 batch1
（quality_momentum_factors.py，21 特征）的关系先盘点，避免重复测试：
  已测已灭不重跑：UMD 2-12（mom_12_1/umd_rank 灭）、12 月夏普动量
    （sharpe252 灭——累计收益/年化波动与 mean/std×√n 秩等价）。
  本批新测 5 特征列（12 月窗口 + FF3 完整版）：
    id252        信息离散度 12 月版（batch1: id60 存活驼峰 / id120 灭——窗口尺度敏感）
    lpm252       部分矩 12 月版（batch1: lpm60 5/5 年最干净存活）
    resid_ff3_252 残差动量文献精确版：PIT 日频 FF3（等权市场/市值中位拆/市净率
                 中位拆，daily_basic 逐日截面）→ 756 日估计 β（严格先于动量窗，
                 零重叠）→ 252 日残差 mean/std（batch1 的 CAPM-126 短版死于
                 年段 2/5，此为规格升级重测）
    ret252_ex_ind 行业调整 12 月版（batch1: 20 日版存活 / 60 日版灭）
    mom_12_3     中间动量剔 3 月极版（close[t−63]/close[t−252]；batch1: mom_6_1
                 存活 / mom_12_1 灭——完成 剔1/剔3 × 6/12 月网格）

预登记闸（Part 1 特征面）：P1 同闸单源复用；n_tests=26（batch1 21 + 本批 5
累计口径，比批内 5 更保守）；存活 → 过滤测试分类（同 batch1）。

预登记闸（Part 2 部署形态——波动率管理动量的真正问法）：
  文献规格是仓位权重 ∝1/σ²（组合层变换，非特征）——用 P2 五闸受控 A/B 测
  （diag.quality_p2_layering 单源复用 _pm/_to_filled/_ann/_taken 与闸结构）：
    Arm A（用户口径）：pos_cap = c·(σ̄²_train/σ60²) clip[0.03,0.10]，c 训练段
      归一到均帽 0.075（均值匹配——P2 教训：线性映射死于敞口水平差）；
    Arm B（batch1 四存活者合成分部署）：方向对齐 z 等权（训练段 2022-24 参数，
      id60/lpm60/ret20_ex_ind/mom_6_1，ρ>0.95 共线去重同 P2）→ 训练 CDF 分位
      → 主型 0.03+0.07p 保守映射 + 均值匹配敏感性 0.075+0.07(p−0.5)。
  五闸：① outer2026 冻结 ann↑ ② 逐年段 Δ≥−0.02 ③ wf1 oos2022 折改善
        ④ 滑点 25bps 增益存活 ⑤ 拥挤日(>80)不降级。违反任何一条=VETO。
  A/B 是部署假设检验，非批量特征筛选，不进 Bonferroni 家族。

用法（仓库根 cwd）：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_momentum_batch2.py
产物：logs/quality/momentum_batch2.json + momentum_batch2_report.md
"""
import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from diag.quality_p1_discriminance import (FOLD_GATES, _portfolio_ann,
                                           evaluate_feature)
from diag.quality_p2_layering import (NEUTRAL_PCT, POS_MIN, POS_SPAN,
                                      TRAIN_YEARS, YEARS_GATE, _ann, _pm,
                                      _taken, _to_filled)

LAKE = "data_lake/a_shares_daily.parquet"
DAILY_BASIC = "data_lake/daily_basic.parquet"
IN_PARQUET = "logs/quality/trades_features.parquet"
OUT_DIR = "logs/quality"
LOAD_FROM = pd.Timestamp("2016-08-15")   # FF3 756d 估计窗 + 252d 动量窗的最早需求
ALPHA = 0.05
N_TESTS_CUM = 26                          # batch1 21 + 本批 5（累计口径）
B2_FEATURES = ["id252", "lpm252", "resid_ff3_252", "ret252_ex_ind", "mom_12_3"]
T0 = time.time()


def _t(tag):
    print(f"[{time.time() - T0:>6.0f}s] {tag}", flush=True)


def _matrix(path, cols, date_from):
    """湖/日基础表 → {col: DataFrame(date×symbol, float32)}。
    索引列走 pyarrow 直读；日期/符号双因子化后 numpy 稀疏填充（比 MultiIndex
    unstack 省内存——2016 起 ~10M 行）。"""
    flt = [("date", ">=", date_from.to_pydatetime())]
    tb = pq.read_table(path, columns=["date", "symbol"] + cols, filters=flt)
    d = pd.to_datetime(tb.column("date").to_pandas())
    s = tb.column("symbol").to_pandas().astype(str)
    dcode, duniq = pd.factorize(d)
    scode, suniq = pd.factorize(s)
    out = {}
    for c in cols:
        m = np.full((len(duniq), len(suniq)), np.nan, dtype="float32")
        m[dcode, scode] = tb.column(c).to_pandas().to_numpy(dtype="float32")
        out[c] = pd.DataFrame(m, index=pd.DatetimeIndex(duniq),
                              columns=list(suniq))
    return out


def _lk(wide, dates, syms):
    """宽表 → 逐笔值（缺位 NaN）。"""
    ri = wide.index.get_indexer(pd.DatetimeIndex(dates))
    ci = wide.columns.get_indexer(pd.Index(syms))
    v = wide.to_numpy()[np.clip(ri, 0, None), np.clip(ci, 0, None)].astype(float)
    v[(ri < 0) | (ci < 0)] = np.nan
    return v


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    trades = pd.read_parquet(IN_PARQUET)
    trades["signal_date"] = pd.to_datetime(trades["signal_date"])
    main_df = trades[trades["pool"] == "main"].copy()
    folds_df = {f: trades[(trades["pool"] == f) & (~trades["embargoed"])].copy()
                for f in FOLD_GATES}
    print(f"[main] {len(main_df)} 笔；folds "
          f"{ {f: len(v) for f, v in folds_df.items()} }", flush=True)

    # ============ 原料矩阵 ============
    _t(f"load lake close from {LOAD_FROM.date()}")
    close = _matrix(LAKE, ["close"], LOAD_FROM)["close"]
    ret = close.pct_change(fill_method=None).astype("float32")
    _t(f"close {close.shape}")

    # —— FF3 日频因子（PIT：daily_basic 逐日截面中位拆，等权组合）——
    _t("load daily_basic mv/pb + build FF3 daily factors")
    db = _matrix(DAILY_BASIC, ["total_mv", "pb"], LOAD_FROM)
    mv = db["total_mv"].reindex(index=close.index, columns=close.columns)
    pb = db["pb"].reindex(index=close.index, columns=close.columns)
    del db
    r_np, mv_np, pb_np = ret.to_numpy(), mv.to_numpy(), pb.to_numpy()
    n_dates, n_syms = r_np.shape
    mkt_s = np.full(n_dates, np.nan, dtype="float64")
    smb_s = np.full(n_dates, np.nan, dtype="float64")
    hml_s = np.full(n_dates, np.nan, dtype="float64")
    for i in range(n_dates):
        r = r_np[i]
        ok = np.isfinite(r) & np.isfinite(mv_np[i]) & np.isfinite(pb_np[i])
        if ok.sum() < 200:
            continue
        rv = r[ok]
        mkt_s[i] = rv.mean()
        med_mv = np.median(mv_np[i][ok])
        small = rv[mv_np[i][ok] <= med_mv]
        big = rv[mv_np[i][ok] > med_mv]
        smb_s[i] = small.mean() - big.mean()
        med_pb = np.median(pb_np[i][ok])
        value = rv[pb_np[i][ok] <= med_pb]      # 低 PB = 高账面市值比
        growth = rv[pb_np[i][ok] > med_pb]
        hml_s[i] = value.mean() - growth.mean()
    fac = pd.DataFrame({"mkt": mkt_s, "smb": smb_s, "hml": hml_s},
                       index=close.index)
    _t(f"FF3 因子 {len(fac.dropna())}/{n_dates} 天有效")

    # ============ Part 1 · 五特征 ============
    _t("wide features: id252/lpm252/mom_12_3 + batch1 复用四特征")
    neg = (ret < 0).astype("float32")
    pos = (ret > 0).astype("float32")
    pn = neg.rolling(252, min_periods=200).mean()
    pp = pos.rolling(252, min_periods=200).mean()
    cum = close / close.shift(252) - 1.0
    id252 = (np.sign(cum) * (pn - pp)).astype("float32")
    del pn, pp, cum, neg, pos
    rn2 = (ret.clip(upper=0) ** 2).astype("float32")
    lpm252 = np.sqrt(rn2.rolling(252, min_periods=200).mean()).astype("float32")
    del rn2
    mom_12_3 = (close.shift(63) / close.shift(252) - 1.0).astype("float32")
    mom_6_1 = (close.shift(21) / close.shift(126) - 1.0).astype("float32")
    vol60 = ret.rolling(60, min_periods=60).std().astype("float32")
    neg60 = (ret < 0).astype("float32")
    pos60 = (ret > 0).astype("float32")
    pn60 = neg60.rolling(60, min_periods=48).mean()
    pp60 = pos60.rolling(60, min_periods=48).mean()
    cum60 = close / close.shift(60) - 1.0
    id60 = (np.sign(cum60) * (pn60 - pp60)).astype("float32")
    del neg60, pos60, pn60, pp60, cum60
    rp2 = (ret.clip(lower=0) ** 2).astype("float32")
    rn2b = (ret.clip(upper=0) ** 2).astype("float32")
    lpm60 = np.sqrt(rn2b.rolling(60, min_periods=48).mean()).astype("float32")
    del rp2, rn2b

    # 行业（batch1 同款：stock_basic 今日截面 110 类，等权行业收益）
    _t("industry ret252_ex_ind + ret20_ex_ind")
    sb = pq.read_table("data_lake/stock_basic.parquet",
                       columns=["ts_code", "industry"]).to_pandas()
    ind_map = dict(zip(sb["ts_code"].astype(str), sb["industry"].astype(str)))
    ind_of = pd.Series([ind_map.get(s, np.nan) for s in close.columns],
                       index=close.columns, dtype=object)
    ind_names = sorted(set(ind_of.dropna().unique()))
    ind_cum = {}      # ind → Series(逐日行业累计收益近似=rolling sum)
    ind_cols = {}
    for name in ind_names:
        cols = ind_of.index[ind_of == name]
        ir = ret[cols].mean(axis=1)
        ind_cols[name] = (ir.rolling(20, min_periods=15).sum(),
                          ir.rolling(252, min_periods=200).sum())
    trades_ind = pd.Series([ind_map.get(s, np.nan) for s in trades["symbol"]],
                           index=trades.index)
    print(f"[industry] 湖内 {int(ind_of.isna().sum())}/{len(close.columns)} 只"
          f"无行业；流水侧缺失 {int(trades_ind.isna().sum())}/{len(trades)}",
          flush=True)

    # —— resid_ff3_252：逐笔 lstsq（β 估计窗 [p−1007, p−252]，动量窗 [p−251, p]）——
    _t("resid_ff3_252 per-trade lstsq")
    fac_np = fac.to_numpy()          # (n_dates, 3)
    fac_ok = np.isfinite(fac_np).all(axis=1)
    date_pos = {d: i for i, d in enumerate(close.index)}
    sym_arr = {}
    for sym in trades["symbol"].unique():
        ci = close.columns.get_loc(sym) if sym in close.columns else None
        if ci is None:
            continue
        col = close.iloc[:, ci]
        ok = col.notna().to_numpy()
        dts = col.index[ok]
        rr = ret.iloc[:, ci].to_numpy()[ok]
        # 符号日期 → 全局因子行号
        fpos = close.index.get_indexer(dts)
        sym_arr[sym] = (dts, rr, fpos, fac_ok)
    resid_ff3 = np.full(len(trades), np.nan)
    t_dates = trades["signal_date"]
    for k, (sym, sd) in enumerate(zip(trades["symbol"], t_dates)):
        a = sym_arr.get(sym)
        if a is None:
            continue
        dts, rr, fpos, fok = a
        p = dts.searchsorted(pd.Timestamp(sd), side="right") - 1
        if p < 252:
            continue
        e0, e1 = max(0, p - 1007), p - 252
        m0, m1 = p - 251, p + 1
        if e1 - e0 < 400 or m1 - m0 < 200:
            continue
        fe, fm = fpos[e0:e1], fpos[m0:m1]
        Xe = fac_np[fe]
        ve = fok[fe]
        Xe = Xe[ve]
        ye = rr[e0:e1][ve]
        if len(ye) < 400:
            continue
        beta, *_ = np.linalg.lstsq(Xe, ye, rcond=None)
        Xm = fac_np[fm]
        vm = fok[fm]
        resid = rr[m0:m1][vm] - Xm[vm] @ beta
        if len(resid) < 200 or resid.std() <= 0:
            continue
        resid_ff3[k] = float(resid.mean() / resid.std())
    _t(f"resid_ff3_252 有效 {np.isfinite(resid_ff3).sum()}/{len(trades)}")

    # —— 逐笔装配 ——
    _t("assemble per-trade features")
    out = pd.DataFrame(index=trades.index)
    td, ts = trades["signal_date"], trades["symbol"]
    out["id252"] = _lk(id252, td, ts)
    out["lpm252"] = _lk(lpm252, td, ts)
    out["mom_12_3"] = _lk(mom_12_3, td, ts)
    out["resid_ff3_252"] = resid_ff3
    ret20 = _lk((close / close.shift(20) - 1.0).astype("float32"), td, ts)
    ret252 = _lk((close / close.shift(252) - 1.0).astype("float32"), td, ts)
    for w, ret_v in ((20, ret20), (252, ret252)):
        col = np.full(len(trades), np.nan)
        for name, (_, s) in ind_cols.items():
            m = (trades_ind == name).to_numpy()
            if m.any():
                col[m] = s.reindex(td[m]).to_numpy()
        out[f"ret{w}_ex_ind"] = ret_v - col
    # batch1 四存活者复用值（Arm B 原料 + 冗余度对照）；ret20_ex_ind 已在上面
    # 的行业循环中落列（w=20 分支）
    out["id60"] = _lk(id60, td, ts)
    out["lpm60"] = _lk(lpm60, td, ts)
    out["mom_6_1"] = _lk(mom_6_1, td, ts)
    out["vol60"] = _lk(vol60, td, ts)

    # —— 四闸（累计 n_tests=26）——
    _t(f"gates n_tests={N_TESTS_CUM}")
    bonf = ALPHA / N_TESTS_CUM
    print(f"[prereg] bonf_alpha={bonf:.2e} |dq51|>=0.5pp 年段≥4/5 wf双折同向",
          flush=True)
    for frame in [main_df] + list(folds_df.values()):
        for c in B2_FEATURES:
            frame[c] = out.loc[frame.index, c].to_numpy()
    results = [evaluate_feature(main_df, folds_df, c, ALPHA, N_TESTS_CUM)
               for c in B2_FEATURES]
    survivors = [r for r in results if r.get("survivor")]
    print(f"[gates] 存活 {len(survivors)}："
          f"{[r['feature'] for r in survivors]}", flush=True)

    # 冗余度：新特征 × batch1 四存活者（先把复用列对齐进 main_df 供 corr 用）
    for c in ["id60", "lpm60", "ret20_ex_ind", "mom_6_1"]:
        main_df[c] = out.loc[main_df.index, c].to_numpy()
    redundancy = {}
    for c in B2_FEATURES:
        s = main_df[c].dropna()
        best, bv = None, 0.0
        for e in ["id60", "lpm60", "ret20_ex_ind", "mom_6_1"]:
            rho = main_df.loc[s.index, c].corr(main_df.loc[s.index, e],
                                               method="spearman")
            if rho == rho and abs(rho) > abs(bv):
                best, bv = e, rho
        redundancy[c] = {"max_abs_rho": round(float(bv), 3), "vs": best}

    # 过滤测试（batch1 同款）
    _t("filter test")
    from discovery.split import Segment, holdout_split
    split = holdout_split()
    full_seg = Segment("full", date(2021, 1, 1), date(2026, 12, 31))
    _dates = pq.read_table(LAKE, columns=["date"]).column("date").to_pandas()
    udates = pd.DatetimeIndex(sorted(_dates.unique()))
    udates = udates[udates >= pd.Timestamp("2021-01-01")]
    pm_freeze = _pm()
    to_filled = lambda d: [{"symbol": r["symbol"], "signal_date": r["signal_date"],
                            "buy_date": r["buy_date"], "exit_date": r["exit_date"],
                            "avg_pnl_pct": r["avg_pnl_pct"], "entry": r["entry"]}
                           for _, r in d.iterrows()]
    base_outer = to_filled(main_df[main_df["signal_date"].apply(split.outer.covers)])
    base_full = to_filled(main_df)
    ann_base_outer = _portfolio_ann(base_outer, split.outer, udates, pm_freeze)
    ann_base_full = _portfolio_ann(base_full, full_seg, udates, pm_freeze)
    print(f"[baseline] outer={ann_base_outer} full={ann_base_full}", flush=True)
    for sv in survivors:
        c, d = sv["feature"], sv["direction"]
        lo_t, hi_t = main_df[c].dropna().quantile([0.3, 0.7])
        worst = (main_df[c] <= lo_t) if d > 0 else (main_df[c] >= hi_t)
        kept = main_df[~worst]
        fo = to_filled(kept[kept["signal_date"].apply(split.outer.covers)])
        ff = to_filled(kept)
        sv["filter_test"] = {
            "drop": "bottom30" if d > 0 else "top30",
            "n_dropped_outer": len(base_outer) - len(fo),
            "outer": _portfolio_ann(fo, split.outer, udates, pm_freeze),
            "full": _portfolio_ann(ff, full_seg, udates, pm_freeze),
        }
        gain = sv["filter_test"]["outer"]["ann"] - ann_base_outer["ann"]
        sv["classification"] = ("filter_candidate" if gain > 0
                                else "layering_candidate")
        print(f"[class] {c}: 外层 Δ{gain:+.3f} → {sv['classification']}",
              flush=True)

    # ============ Part 2 · 部署形态 A/B（P2 五闸） ============
    _t("deployment A/B arms (P2 five gates)")
    for frame in [main_df] + list(folds_df.values()):
        for c in ["vol60", "id60", "lpm60", "ret20_ex_ind", "mom_6_1"]:
            if c not in frame.columns:
                frame[c] = out.loc[frame.index, c].to_numpy()
    fold1 = folds_df["wf1_2020_21"]
    train_main = main_df[(main_df["year"] >= TRAIN_YEARS[0])
                         & (main_df["year"] <= TRAIN_YEARS[1])]
    outer = split.outer
    seg22 = Segment("y2022", date(2022, 1, 1), date(2022, 12, 31))

    fixed = _to_filled(main_df)
    pm_f = _pm()

    # —— Arm A：1/σ² 逆方差（vol60），训练段归一均值匹配 ——
    sig2_train = train_main["vol60"].astype(float).dropna() ** 2
    med2 = float(sig2_train.median())
    ratio_train = med2 / sig2_train
    c_norm = 0.075 / float(np.clip(ratio_train, 0.03 / 0.075, 0.10 / 0.075).mean())

    def arm_a_caps(df):
        s2 = df["vol60"].astype(float).to_numpy() ** 2
        caps = np.where(np.isfinite(s2), c_norm * med2 / s2, 0.075)
        return np.clip(caps, POS_MIN, 0.10)

    # —— Arm B：batch1 四存活者合成分（z 对齐等权，训练 CDF 分位）——
    b_feats = {"id60": 1, "lpm60": -1, "ret20_ex_ind": -1, "mom_6_1": -1}
    _ks = list(b_feats)
    for f in list(_ks):
        for g in _ks:
            if g == f or f not in b_feats or g not in b_feats:
                continue
            rho = main_df[f].corr(main_df[g], method="spearman")
            if rho is not None and abs(rho) > 0.95:
                del b_feats[g]
                _ks = list(b_feats)
                break
    print(f"[armB] 共线去重后特征 {b_feats}", flush=True)
    zparams = {}
    for f in b_feats:
        s = train_main[f].astype(float).dropna()
        if len(s) >= 200 and s.std() > 0:
            zparams[f] = (float(s.mean()), float(s.std()))
    keep_feats = list(zparams)

    def arm_b_scores(df):
        """方向对齐 z 等权（可用特征均值；全缺 → NaN=中性）。向量化。"""
        zs = np.full((len(df), len(keep_feats)), np.nan)
        for j, f in enumerate(keep_feats):
            mu, sd = zparams[f]
            v = pd.to_numeric(df[f], errors="coerce").to_numpy(dtype=float)
            zs[:, j] = (v - mu) / sd * b_feats[f]
        with np.errstate(invalid="ignore"):
            return np.where(np.isnan(zs).all(axis=1), np.nan,
                            np.nanmean(zs, axis=1))

    def _score_cdf(scores):
        """分数 → 训练段 CDF 分位（NaN → 中性 0.5）。"""
        p = np.full(len(scores), NEUTRAL_PCT)
        ok = np.isfinite(scores)
        p[ok] = np.searchsorted(train_scores, scores[ok], side="right") \
            / len(train_scores)
        return p

    # 训练段分数 → CDF（一次性构建）
    train_scores = np.sort(arm_b_scores(train_main))
    train_scores = train_scores[np.isfinite(train_scores)]

    def five_gates(name, caps_fn, extra=None):
        """A/B 通用五闸。caps_fn(df)->pos_cap 数组。返回 dict。"""
        dfq = main_df.copy()
        dfq["pos_cap"] = caps_fn(dfq)
        quality = _to_filled(dfq)
        pm_q = _pm(quality_alloc=True)
        f_out = _ann([t for t in fixed if outer.covers(t["signal_date"])],
                     outer, udates, pm_f)
        q_out = _ann([t for t in quality if outer.covers(t["signal_date"])],
                     outer, udates, pm_q)
        g1 = q_out["ann"] > f_out["ann"]
        yearly = {}
        for y in YEARS_GATE:
            seg = Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
            fy = _ann([t for t in fixed if seg.covers(t["signal_date"])],
                      seg, udates, pm_f)
            qy = _ann([t for t in quality if seg.covers(t["signal_date"])],
                      seg, udates, pm_q)
            yearly[y] = {"fixed": fy, "quality": qy,
                         "delta": round(qy["ann"] - fy["ann"], 4)}
        g2 = all(v["delta"] >= -0.02 for v in yearly.values())
        fold_q = fold1.copy()
        fold_q["pos_cap"] = caps_fn(fold_q)
        f_f1 = _ann(_to_filled(fold1), seg22, udates, pm_f)
        q_f1 = _ann(_to_filled(fold_q), seg22, udates, pm_q)
        g3 = q_f1["ann"] > f_f1["ann"]
        f25 = _ann([t for t in fixed if outer.covers(t["signal_date"])],
                   outer, udates, _pm(slippage_bps=25))
        q25 = _ann([t for t in quality if outer.covers(t["signal_date"])],
                   outer, udates, _pm(slippage_bps=25, quality_alloc=True))
        g4 = q25["ann"] > f25["ann"]
        taken_f = _taken([t for t in fixed if outer.covers(t["signal_date"])], pm_f)
        taken_q = _taken([t for t in quality if outer.covers(t["signal_date"])], pm_q)
        mf = [t["avg_pnl_pct"] for t in taken_f if t["same_day_n"] > 80]
        mq = [t["avg_pnl_pct"] for t in taken_q if t["same_day_n"] > 80]
        m_f = round(float(np.mean(mf)), 2) if mf else None
        m_q = round(float(np.mean(mq)), 2) if mq else None
        g5 = (m_f is not None and m_q is not None and m_q >= m_f - 0.5)
        verdict = "PASS" if (g1 and g2 and g3 and g4 and g5) else "VETO"
        res = {"arm": name, "gates": {
            "g1_ann_up": {"fixed": f_out, "quality": q_out,
                          "delta": round(q_out["ann"] - f_out["ann"], 4),
                          "pass": g1},
            "g2_yearly": {y: v for y, v in yearly.items()} | {"pass": g2},
            "g3_wf2022": {"fixed": f_f1, "quality": q_f1,
                          "delta": round(q_f1["ann"] - f_f1["ann"], 4),
                          "pass": g3},
            "g4_slip25": {"fixed": f25, "quality": q25,
                          "delta": round(q25["ann"] - f25["ann"], 4),
                          "pass": g4},
            "g5_crowded": {"fixed_mean": m_f, "quality_mean": m_q,
                           "pass": g5}},
            "verdict": verdict,
            "mean_pos_cap": round(float(dfq["pos_cap"].mean()), 4),
            "extra": extra or {}}
        print(f"[{name}] ①{g1:d} ②{g2:d} ③{g3:d} ④{g4:d} ⑤{g5:d} → "
              f"{verdict} (outer Δ{q_out['ann'] - f_out['ann']:+.3f}, "
              f"均帽 {dfq['pos_cap'].mean():.4f})", flush=True)
        return res

    arm_a = five_gates(
        "A_inv_var_vol60",
        lambda df: arm_a_caps(df),
        extra={"map": f"clip({c_norm:.4f}·σ̄²/σ60², 0.03, 0.10)",
               "note": "文献 1/σ² 缩放的带限部署形态；训练段归一均帽 0.075"})

    # Arm B 主型（保守映射）+ 均值匹配敏感性
    def arm_b_caps_conservative(df):
        return np.clip(POS_MIN + POS_SPAN * _score_cdf(arm_b_scores(df)),
                       POS_MIN, 0.10)

    def arm_b_caps_mm(df):
        return np.clip(0.075 + 0.07 * (_score_cdf(arm_b_scores(df)) - 0.5),
                       0.03, 0.10)

    arm_b = five_gates(
        "B_batch1_composite_conservative", arm_b_caps_conservative,
        extra={"features": b_feats, "map": "0.03+0.07p (P2 原版保守)"})
    arm_b_mm = five_gates(
        "B_batch1_composite_mean_matched", arm_b_caps_mm,
        extra={"map": "0.075+0.07(p-0.5)（均值匹配——P2 教训的敞口中性版）"})

    # ============ 产物 ============
    _t("write outputs")
    report = {
        "preregistration": {
            "part1": [f"G1 Bonferroni p<{bonf:.2e} (n_tests=26 累计) & "
                      f"|Q5-Q1|>=0.5pp", "G2 >=4/5 年同向", "G3 wf 双折同向"],
            "part2": "P2 五闸（①ann↑ ②逐年≥−0.02 ③wf2022 ④滑点25bps ⑤拥挤日）",
            "already_dead_no_rerun": {"umd_2_12": "mom_12_1/umd_rank batch1 灭",
                                      "sharpe_12m": "sharpe252 batch1 灭"},
        },
        "baseline_ann": {"outer2026_freeze": ann_base_outer,
                         "full2021_26_freeze": ann_base_full},
        "survivors": survivors,
        "redundancy_vs_batch1_survivors": redundancy,
        "all_features": results,
        "deployment_arms": [arm_a, arm_b, arm_b_mm],
        "arm_b_features_after_dedup": b_feats,
        "arm_a_map": {"c_norm": round(c_norm, 4), "sigma2_train_median": med2},
        "ff3_factor_days": int(fac.notna().all(axis=1).sum()),
        "resid_ff3_coverage": int(np.isfinite(resid_ff3).sum()),
        "generated_at": pd.Timestamp.now().isoformat(),
    }
    with open(os.path.join(OUT_DIR, "momentum_batch2.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=str)

    L, ap = [], None
    ap = L.append
    ap("# 文献动量因子 batch2（12 月精确参数版 · 2026-08-28）")
    ap("")
    ap("> Part 1 特征面：5 新列过 P1 同闸（n_tests=26 累计 Bonferroni）；"
       "Part 2 部署形态：波动率管理 1/σ² 与 batch1 四存活者合成分各过 P2 五闸。")
    ap(f"> 基线：outer2026 冻结口径 ann {ann_base_outer['ann']:+.1%} / "
       f"taken {ann_base_outer['n_taken']}；全期 {ann_base_full['ann']:+.1%}。")
    ap(f"> 已测已灭不重跑：UMD 2-12（batch1 mom_12_1/umd_rank）、"
       f"12 月夏普动量（batch1 sharpe252，秩等价）。")
    ap("")
    ap(f"## Part 1 存活 {len(survivors)} 个：" +
       ("、".join(f"`{s['feature']}`" for s in survivors) if survivors else "（无）"))
    ap("")
    for s in survivors:
        ft = s.get("filter_test", {})
        ap(f"### {s['feature']}（方向 {'高好' if s['direction'] > 0 else '低好'}，"
           f"IC={s['ic']:+.4f}，ΔQ5−Q1={s['dq51']:+.2f}pp）")
        ap("")
        ap(f"- 五分位均值：{s['q_means']}")
        ap(f"- 年段：{s['year_contrasts']} → {s['years_same']}/5；"
           f"wf 折：{s['fold_contrasts']}")
        ap(f"- 过滤测试：outer ann {ft['outer']['ann']:+.1%}（Δ"
           f"{ft['outer']['ann'] - ann_base_outer['ann']:+.3f}）"
           f"→ **{s['classification']}**")
        ap("")
    rows = [{"feature": r["feature"], "n": r.get("n"), "IC": r.get("ic"),
             "ΔQ5−Q1": r.get("dq51"), "G1": r.get("gate_G1", False),
             "年同向": f"{r.get('years_same', 0)}/5" if r.get("year_contrasts") else "",
             "G3双折": "过" if r.get("gate_G3") else str(r.get("fold_contrasts")),
             "存活": r.get("survivor", False)} for r in results]
    ap("## Part 1 全特征明细")
    ap("")
    ap(pd.DataFrame(rows).to_string(index=False))
    ap("")
    ap("## Part 2 部署形态 A/B（P2 五闸）")
    ap("")
    for arm in (arm_a, arm_b, arm_b_mm):
        g = arm["gates"]
        ap(f"### {arm['arm']} → **{arm['verdict']}**"
           f"（均帽 {arm['mean_pos_cap']:.4f}）")
        ap("")
        ap("| 闸 | 固定臂 | 质量臂 | Δ | 过 |")
        ap("|---|---|---|---|---|")
        ap(f"| ① outer ann | {g['g1_ann_up']['fixed']['ann']:+.1%} | "
           f"{g['g1_ann_up']['quality']['ann']:+.1%} | "
           f"{g['g1_ann_up']['delta']:+.3f} | "
           f"{'✅' if g['g1_ann_up']['pass'] else '❌'} |")
        ap(f"| ④ 滑点25bps | {g['g4_slip25']['fixed']['ann']:+.1%} | "
           f"{g['g4_slip25']['quality']['ann']:+.1%} | "
           f"{g['g4_slip25']['delta']:+.3f} | "
           f"{'✅' if g['g4_slip25']['pass'] else '❌'} |")
        ap(f"| ③ wf2022 | {g['g3_wf2022']['fixed']['ann']:+.1%} | "
           f"{g['g3_wf2022']['quality']['ann']:+.1%} | "
           f"{g['g3_wf2022']['delta']:+.3f} | "
           f"{'✅' if g['g3_wf2022']['pass'] else '❌'} |")
        yr = " ".join(f"{y}:{v['delta']:+.3f}"
                      for y, v in g["g2_yearly"].items() if y != "pass")
        ap(f"| ② 逐年 Δ | — | — | {yr} | "
           f"{'✅' if g['g2_yearly']['pass'] else '❌'} |")
        ap(f"| ⑤ 拥挤日均值 | {g['g5_crowded']['fixed_mean']} | "
           f"{g['g5_crowded']['quality_mean']} | — | "
           f"{'✅' if g['g5_crowded']['pass'] else '❌'} |")
        ap("")
    ap("## 冗余度（新特征 × batch1 存活者）")
    ap("")
    ap(pd.DataFrame([{"feature": c, **v}
                     for c, v in redundancy.items()]).to_string(index=False))
    ap("")
    with open(os.path.join(OUT_DIR, "momentum_batch2_report.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[done] {OUT_DIR}/momentum_batch2.json + momentum_batch2_report.md "
          f"(总用时 {(time.time() - T0) / 60:.0f}min)", flush=True)


if __name__ == "__main__":
    main()
