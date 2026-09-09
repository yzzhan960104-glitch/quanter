# -*- coding: utf-8 -*-
"""信号质量 · 因子动物园 波3 大扩容（2026-08-29 · 用户指令「把你知道的因子
都测试一遍」）。

波1/2 已测 164 项（~60 族×双轴）。本波补齐知识面内、当前湖可算的全部剩余
家族（价格路径统计/ Kaufman ER/ KDJ·WR·DMI/ 量价资金 OBV·AD·CMF·MFI·
上量比/ 四种 OHLC 波动率/ 尾部与极值日/ 截面相对(市场超额/行业秩/α/秩动量)/
估值(pe·ps·流通比)/ A股特色(涨停·炸板·一字板·次新·低价·整数关·月末月初)/
moneyflow 分档(散户/主力大单)/ 几何余量(颈线相对位/止损距)），约 80 新列
×双轴。不可算族（数据面硬约束，留档不测）：订单簿/分时族（OFI/MLOFI/ITSM/
真实半方差）、基本面季频族（fina_* 未接 PIT 财报对齐）、分析师/舆情/另类、
跨资产族。

══════════ 预登记（先于运行写死）══════════
闸与波1 完全一致（G1/G2/G3 + G5 结构闸全局秩同日置换）；本波预算
N_W3 = 220（新列×双轴≈180 + 噪声校准不占预算），bonf = 0.05/220=2.27e-4。
存活=登记册条目（追加 logs/quality/factor_zoo/registry.jsonl，同键跳过）。
收尾必跑：**复合闸噪声校准**——120 个同日重排噪声列过含 G5 的全闸，实测
经验假阳性率（波1 的 37.5% 是无 G5 旧漏斗的读数；本读数回答大扩容后的
可信度）。产物：logs/quality/factor_zoo/{wave3_report.md, wave3_noise_fpr.json}。

用法：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_zoo_wave3.py
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
from diag.quality_zoo_loop import (FOLD_KEYS, _contrast, _quintile,
                                    _structure, build_bases, evaluate)

OUT_DIR = "logs/quality/factor_zoo"
REG_PATH = os.path.join(OUT_DIR, "registry.jsonl")
ALPHA = 0.05
N_W3 = 220
BONO = ALPHA / N_W3
RNG = np.random.default_rng(202608293)
T0 = time.time()


def _log(m):
    print(f"[{time.time() - T0:>6.0f}s] {m}", flush=True)


def _roll(df, w, fn):
    return fn(df.rolling(w, min_periods=int(w * 0.8)))


def gen_wave3(B, F):
    """波3 全族。F = fam_bases(B) 缓存。返回 {列名: wide}。"""
    close, open_, high, low = B["close"], B["open"], B["high"], B["low"]
    volume, ret, tr = B["volume"], B["ret"], B["tr"]
    out = {}
    f32 = lambda x: x.astype("float32")

    # —— A. 价格路径统计 ——
    for w in (20, 60):
        diff = close.diff().abs()
        out[f"kaufman_er{w}"] = f32((close - close.shift(w)).abs()
                                    / (diff.rolling(w, min_periods=int(w * .8))
                                       .sum() + 1e-12))
    out["ac1_60"] = ret.rolling(60, min_periods=48).corr(ret.shift(1))
    out["ac5_60"] = ret.rolling(60, min_periods=48).corr(ret.shift(5))
    sgn_chg = (np.sign(ret) != np.sign(ret.shift(1))).astype("float32")
    out["alternation20"] = sgn_chg.rolling(20, min_periods=15).mean()
    out["pos_share20"] = (ret > 0).astype("float32").rolling(20, min_periods=15).mean()
    for w in (60, 120):
        dd = close / close.rolling(w, min_periods=int(w * .8)).max() - 1.0
        out[f"dd{w}"] = f32(dd)
        out[f"ulcer{w}"] = f32(np.sqrt((dd.astype("float32") ** 2)
                                       .rolling(w, min_periods=int(w * .8)).mean()))
    out["calmar20_60"] = f32((close / close.shift(20) - 1.0)
                             / (out["ulcer60"].abs() + 1e-4))
    # 衰减加权动量（近端权重 1，远端 0.5）
    r10 = close / close.shift(10) - 1.0
    r20 = close / close.shift(20) - 1.0
    r60 = close / close.shift(60) - 1.0
    out["decay_mom20"] = f32(r10 + 0.5 * (r20 - r10))
    out["decay_mom60"] = f32(r20 + 0.5 * (r60 - r20))
    out["on_in_ratio20"] = f32(F["on_gap"].rolling(20, min_periods=15).sum()
                               / (F["in_ret"].rolling(20, min_periods=15).sum()
                                  .abs() + 1e-6))

    # —— B. 传统技术指标（前复权自算）——
    ll9 = low.rolling(9, min_periods=7).min()
    hh9 = high.rolling(9, min_periods=7).max()
    rsv = (close - ll9) / (hh9 - ll9 + 1e-12)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    out["kdj_j"] = f32(3 * k - 2 * d)
    out["kdj_kd"] = f32(k - d)
    out["wr14"] = f32((high.rolling(14, min_periods=10).max() - close)
                      / (high.rolling(14, min_periods=10).max()
                         - low.rolling(14, min_periods=10).min() + 1e-12))
    up_dm = high.diff().clip(lower=0)
    dn_dm = (-low.diff()).clip(lower=0)
    pdi = up_dm.rolling(14, min_periods=10).mean()
    mdi = dn_dm.rolling(14, min_periods=10).mean()
    out["dmi_gap60"] = f32((pdi - mdi) / (pdi + mdi + 1e-12)).rolling(
        60, min_periods=40).mean()
    ma5 = close.rolling(5, min_periods=4).mean()
    ma10 = close.rolling(10, min_periods=8).mean()
    ma20 = close.rolling(20, min_periods=15).mean()
    ma60 = close.rolling(60, min_periods=48).mean()
    stack = ((close > ma5).astype("int8") + (close > ma10).astype("int8")
             + (close > ma20).astype("int8") + (close > ma60).astype("int8"))
    out["ma_stack"] = stack.astype("float32")
    atr14 = (F["up_shadow"] * 0 + (high - low).rolling(14, min_periods=10).mean())
    out["ma_spread_5_20"] = f32((ma5 - ma20) / (atr14 + 1e-12))
    out["ma20_slope5"] = f32((ma20 - ma20.shift(5)) / (atr14 + 1e-12))

    # —— C. 量价资金流 ——
    vv = volume.astype("float64")
    upv = (vv * (ret > 0)).rolling(20, min_periods=15).sum()
    allv = vv.rolling(20, min_periods=15).sum()
    out["upvol_ratio20"] = f32(upv / (allv + 1e-12))
    obv = (np.sign(ret.fillna(0)) * vv).cumsum()
    out["obv_slope20"] = f32((obv - obv.shift(20)) / (allv + 1e-12))
    ad = (F["cpos"].astype("float64") * vv).cumsum()
    out["ad_slope20"] = f32((ad - ad.shift(20)) / (allv + 1e-12))
    out["cmf20"] = f32((F["cpos"].astype("float64") * vv)
                       .rolling(20, min_periods=15).sum() / (allv + 1e-12))
    tp = (high + low + close) / 3.0
    mf = tp * vv
    tp_up = (tp > tp.shift(1)).astype("float32")
    pmf = (mf * tp_up).rolling(14, min_periods=10).sum()
    nmf = (mf * (1 - tp_up)).rolling(14, min_periods=10).sum()
    out["mfi14"] = f32(100 - 100 / (1 + pmf / (nmf + 1e-12)))
    out["absret_vol_corr20"] = ret.abs().rolling(20, min_periods=15).corr(vv)
    vma20 = volume.rolling(20, min_periods=15).mean()
    out["hi_vol_cnt20"] = (volume > 2 * vma20).astype("float32").rolling(
        20, min_periods=15).sum()
    out["vol_top1_share20"] = f32(volume.rolling(20, min_periods=15).max()
                                  / (allv + 1e-12))
    out["quiet_share20"] = (volume < 0.7 * vma20).astype("float32").rolling(
        20, min_periods=15).mean()
    out["shrink_share20"] = (volume < volume.shift(1)).astype("float32").rolling(
        20, min_periods=15).mean()

    # —— D. OHLC 波动率家族 ——
    rng_ = (high - low).astype("float32")
    log_hl = np.log(high / low).astype("float32") ** 2
    log_co = np.log(close / open_).astype("float32") ** 2
    for w in (20, 60):
        mp = w * 0.8
        out[f"pk_vol{w}"] = f32(np.sqrt(log_hl.rolling(w, min_periods=int(mp))
                                        .mean() / (4 * np.log(2))))
        out[f"gk_vol{w}"] = f32(np.sqrt((0.5 * log_hl
                                         - (2 * np.log(2) - 1) * log_co)
                                        .rolling(w, min_periods=int(mp)).mean()))
    rs = np.log(high / close) * np.log(high / open_) + np.log(low / close) \
        * np.log(low / open_)
    out["rs_vol20"] = f32(np.sqrt(rs.astype("float32")
                                   .rolling(20, min_periods=15).mean()))
    neg_sq = (ret.clip(upper=0) ** 2).astype("float32")
    all_sq = (ret ** 2).astype("float32")
    out["semi_ratio20"] = f32(np.sqrt(neg_sq.rolling(20, min_periods=15).mean())
                              / np.sqrt(all_sq.rolling(20, min_periods=15).mean()
                                        + 1e-12))
    out["tail_spread60"] = f32(ret.rolling(60, min_periods=48)
                               .quantile(0.95) - ret.rolling(60, min_periods=48)
                               .quantile(0.05))
    out["extreme_share20"] = (ret.abs() > 0.03).astype("float32").rolling(
        20, min_periods=15).mean()

    # —— E. 截面相对 ——
    mkt = B["mkt"]
    for w in (20, 60):
        out[f"ex_mkt{w}"] = f32((close / close.shift(w) - 1.0)
                                - mkt.rolling(w, min_periods=int(w * .8)).sum())
    xmu = ret.mul(mkt, axis=0).rolling(250, min_periods=200).mean()
    rm_ = ret.rolling(250, min_periods=200).mean()
    mm_ = mkt.rolling(250, min_periods=200).mean()
    mv_ = mkt.rolling(250, min_periods=200).var()
    beta = xmu.sub(rm_.mul(mm_, axis=0)).div(mv_, axis=0)
    for w in (60, 120):
        out[f"alpha{w}"] = f32(ret.rolling(w, min_periods=int(w * .8)).mean()
                               - beta.mul(mkt.rolling(w, min_periods=int(w * .8))
                                          .mean(), axis=0))
    xmu2 = ret.mul(mkt, axis=0).rolling(60, min_periods=48).mean()
    rm2 = ret.rolling(60, min_periods=48).mean()
    cov60 = xmu2.sub(rm2.mul(mkt.rolling(60, min_periods=48).mean(), axis=0))
    out["mkt_corr60"] = (cov60 / (ret.rolling(60, min_periods=48).std()
                                   * mkt.rolling(60, min_periods=48).std()
                                   + 1e-12)).astype("float32")
    del xmu2, rm2, cov60
    # 行业内秩（分行业块计算，避免全湖 stack 的 630MB 中间对象）与秩动量
    ind_of = B["ind_of"]
    r60 = close / close.shift(60) - 1.0
    r20r = close / close.shift(20) - 1.0
    ind_rank = pd.DataFrame(np.nan, index=close.index, columns=close.columns,
                            dtype="float32")
    for name in sorted(set(ind_of.dropna().unique())):
        cols = list(ind_of.index[ind_of == name])
        ind_rank[cols] = r60[cols].rank(axis=1, pct=True).astype("float32") \
            .to_numpy()
    out["ind_rank60"] = ind_rank
    rk20 = r20r.rank(axis=1, pct=True)
    out["rank_mom20"] = f32(rk20 - r20r.shift(20).rank(axis=1, pct=True))
    # 截面价格位置
    out["log_price_rank"] = np.log(close + 1e-6).rank(axis=1, pct=True) \
        .astype("float32")
    out["round_num_dist"] = f32((close / close.round(0) - 1.0).abs())

    # —— F. 估值与流通（daily_basic）——
    db = _matrix("data_lake/daily_basic.parquet",
                 ["pe_ttm", "ps", "total_mv"], pd.Timestamp("2019-06-01"))
    pe_ttm = db["pe_ttm"].reindex(index=close.index, columns=close.columns)
    ps = db["ps"].reindex(index=close.index, columns=close.columns)
    tmv = db["total_mv"].reindex(index=close.index, columns=close.columns)
    out["wic_pe"] = pe_ttm.rank(axis=1, pct=True).astype("float32")
    out["wic_ps"] = ps.rank(axis=1, pct=True).astype("float32")
    out["pe_chg60"] = (pe_ttm / pe_ttm.shift(60) - 1.0).astype("float32")
    out["float_ratio"] = (B["cmv"] / (tmv + 1e-6)).astype("float32")
    out["tr_std60"] = tr.rolling(60, min_periods=48).std().astype("float32")
    out["tr_skew60"] = tr.rolling(60, min_periods=48).skew().astype("float32")
    out["vol20_pct252"] = (ret.rolling(20).std()
                           / (ret.rolling(252, min_periods=200).std() + 1e-12)
                           ).astype("float32")

    # —— G. A 股特色（涨停/炸板/一字/次新）——
    lp = close * (1.0 + F["limit_dist"].astype("float32"))
    limit_up = (close >= lp * 0.998).astype("float32")
    limit_touch = ((high >= lp) & (close < lp * 0.998)).astype("float32")
    one_board = ((open_ >= lp * 0.998) & (low >= lp * 0.998)
                 & (close >= lp * 0.998)).astype("float32")
    for w, tag in ((60, "60"), (20, "20")):
        out[f"limit_up_cnt{tag}"] = limit_up.rolling(w, min_periods=int(w * .8)).sum()
        out[f"limit_touch_cnt{tag}"] = limit_touch.rolling(
            w, min_periods=int(w * .8)).sum()
    out["one_board_cnt60"] = one_board.rolling(60, min_periods=48).sum()
    # 次新：上市至今天数（湖首条有效收盘；float32 直填防 630MB 中间对象）
    fp = close.apply(lambda c: c.first_valid_index()).reindex(close.columns)
    fdays = pd.to_datetime(fp).values.astype("datetime64[D]")
    valid = ~np.isnat(fdays)
    base = close.index.values.astype("datetime64[D]")
    age = np.full(close.shape, np.nan, dtype="float32")
    if valid.any():
        diff = (base[:, None] - fdays[None, :]).astype("float64")
        age[:, valid] = diff[:, valid].astype("float32")
    out["listing_age"] = pd.DataFrame(age, index=close.index,
                                      columns=close.columns)

    # —— H. moneyflow 分档 ——
    try:
        mfcols = ["buy_sm_amount", "sell_sm_amount", "buy_elg_amount",
                  "sell_elg_amount"]
        mfw = _matrix("data_lake/moneyflow.parquet", mfcols,
                      pd.Timestamp("2019-06-01"))
        amt = B["amount"].astype("float32")
        sm_net = (mfw["buy_sm_amount"] - mfw["sell_sm_amount"]) \
            .reindex(index=close.index, columns=close.columns)
        elg_net = (mfw["buy_elg_amount"] - mfw["sell_elg_amount"]) \
            .reindex(index=close.index, columns=close.columns)
        out["sm_net20"] = (sm_net / (amt + 1.0)).astype("float32").rolling(
            20, min_periods=15).mean()
        out["elg_net20"] = (elg_net / (amt + 1.0)).astype("float32").rolling(
            20, min_periods=15).mean()
        out["elg_net60"] = (elg_net / (amt + 1.0)).astype("float32").rolling(
            60, min_periods=48).mean()
        out["elg_pos_share20"] = (elg_net > 0).astype("float32").rolling(
            20, min_periods=15).mean()
    except Exception as e:
        _log(f"[wave3] moneyflow 分档跳过: {e}")

    return {k: v.astype("float32") for k, v in out.items()
            if isinstance(v, pd.DataFrame)}


def gen_trade_level(B):
    """直接用 parquet 既有列的几何/日历特征（trade 级，无需宽表）。"""
    m, folds, fm = B["main_df"], B["folds"], B["fm_inc"]
    cols = {}
    for df in (m, *folds.values(), fm):
        df["neck_rel"] = df["neckline"] / df["entry"]
        df["h_rel"] = df["H"] / df["entry"]
        df["risk_pct_v"] = pd.to_numeric(df["risk_pct"], errors="coerce")
        sd = pd.to_datetime(df["signal_date"])
        df["is_monday"] = (sd.dt.dayofweek == 0).astype("float32")
        df["is_friday"] = (sd.dt.dayofweek == 4).astype("float32")
        dim = sd.dt.days_in_month
        df["turn_of_month"] = ((sd.dt.day <= 3) | (sd.dt.day >= dim - 3)) \
            .astype("float32")
    for c in ("neck_rel", "h_rel", "risk_pct_v", "is_monday", "is_friday",
              "turn_of_month"):
        cols[c] = {"main": m[c].to_numpy(dtype=float),
                   **{f: folds[f][c].to_numpy(dtype=float) for f in FOLD_KEYS},
                   "fm_inc": fm[c].to_numpy(dtype=float)}
    return cols


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    _log(f"prereg N_W3={N_W3} bonf={BONO:.2e}")
    B = build_bases()
    from diag.quality_zoo_loop import fam_bases
    F = fam_bases(B)
    main_df, folds, fm_inc = B["main_df"], B["folds"], B["fm_inc"]

    tested = set()
    if os.path.exists(REG_PATH):
        for ln in open(REG_PATH, encoding="utf-8"):
            try:
                r = json.loads(ln)
                tested.add((r["feature"], r["target"]))
            except Exception:
                pass

    survivors = []

    def eval_and_register(col, values):
        # 跳过已测键时也挂列（噪声校准基底需要跨进程续跑的列在场）
        fm_inc[col] = values["fm_inc"]
        if col not in main_df.columns:
            main_df[col] = values["main"]
        for f in FOLD_KEYS:
            if col not in folds[f].columns:
                folds[f][col] = values[f]
        for target in ("avg_pnl_pct", "occ"):
            if (col, target) in tested:
                continue
            res = evaluate(col, target, values, main_df, folds, RNG, BONO)
            with open(REG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
            tested.add((col, target))
            if res.get("survivor"):
                fv = values["fm_inc"]
                ok = np.isfinite(fv)
                if ok.sum() > 200:
                    sub = fm_inc.loc[ok]
                    lo_t, hi_t = pd.Series(fv[ok]).quantile([0.3, 0.7])
                    lo = sub.loc[sub[col] <= lo_t, target]
                    hi = sub.loc[sub[col] >= hi_t, target]
                    res["fm_inc_contrast"] = round(float(hi.mean() - lo.mean()), 3) \
                        if len(lo) >= 20 and len(hi) >= 20 else None
                survivors.append(res)
                _log(f"  ★ {col}×{target}: IC {res['ic']} yrs {res['years_same']}/5"
                     f" wic {res['within_date_ic']} fmΔ {res.get('fm_inc_contrast')}")
            elif res.get("gate_G1") and res.get("gate_G2") and res.get("gate_G3"):
                _log(f"  ~ {col}×{target}: 三闸过 G5 拒 p={res['struct_emp_p']}"
                     f" wic={res['within_date_ic']}")

    # —— 波3 宽表族 ——
    _log("gen wave3 families")
    fams = gen_wave3(B, F)
    _log(f"wave3 列数 {len(fams)}")
    t_f = time.time()
    for i, (k, wide) in enumerate(sorted(fams.items())):
        try:
            vals = {"main": _lk(wide, main_df["signal_date"], main_df["symbol"]),
                    **{f: _lk(wide, folds[f]["signal_date"], folds[f]["symbol"])
                       for f in FOLD_KEYS},
                    "fm_inc": _lk(wide, fm_inc["signal_date"], fm_inc["symbol"])}
            eval_and_register(k, vals)
        except Exception as e:
            _log(f"  ! {k} 失败: {e}")
        if (i + 1) % 15 == 0:
            _log(f"  {i + 1}/{len(fams)}（{time.time() - t_f:.0f}s）")
    del fams

    # —— trade 级（几何/日历）——
    _log("trade-level features")
    for c, vals in gen_trade_level(B).items():
        try:
            eval_and_register(c, vals)
        except Exception as e:
            _log(f"  ! {c} 失败: {e}")

    # —— 噪声校准（含 G5 复合闸）——
    _log("noise calibration (compound gates incl G5)")
    n_pass, n_test = 0, 0
    # 基底取本进程已挂列（跨进程列名不在 main_df——波1 列在独立进程里）
    cand = ["kaufman_er20", "gk_vol60", "tr_std60", "elg_pos_share20",
            "dd60", "upvol_ratio20"]
    bases = [c for c in cand if c in main_df.columns]
    if not bases:
        bases = [c for c in main_df.columns
                 if main_df[c].dtype.kind in "fi"
                 and not c.startswith("__")][:4]
    from diag.quality_zoo_loop import B_PERM
    for rep in range(30):
        for bcol in bases:
            col = f"__noise3_{bcol}_{rep}"
            v = main_df[bcol].to_numpy(dtype=float, copy=True)
            ok = np.isfinite(v)
            d = main_df["signal_date"].to_numpy()
            dcode = pd.factorize(d)[0]
            for g in np.unique(dcode):
                m_ = (dcode == g) & ok
                if m_.sum() > 1:
                    v[m_] = v[m_][RNG.permutation(int(m_.sum()))]
            vals = {"main": v,
                    **{f: np.full(len(folds[f]), np.nan) for f in FOLD_KEYS},
                    "fm_inc": np.full(len(fm_inc), np.nan)}
            # folds 也要同日重排（否则 G3 恒真/恒假失真）
            for f in FOLD_KEYS:
                vf = folds[f][bcol].to_numpy(dtype=float, copy=True)
                okf = np.isfinite(vf)
                df_ = pd.factorize(folds[f]["signal_date"].to_numpy())[0]
                for g in np.unique(df_):
                    m_ = (df_ == g) & okf
                    if m_.sum() > 1:
                        vf[m_] = vf[m_][RNG.permutation(int(m_.sum()))]
                vals[f] = vf
            res = evaluate(col, "avg_pnl_pct", vals, main_df, folds, RNG, BONO)
            n_test += 1
            if res.get("survivor"):
                n_pass += 1
                _log(f"  ⚠ 噪声过闸: {col} p={res['struct_emp_p']}")
        if (rep + 1) % 10 == 0:
            _log(f"  noise {rep + 1}/30: pass {n_pass}/{n_test}")
    fpr = n_pass / n_test if n_test else None
    fpr_s = f"{fpr:.2%}" if fpr is not None else "n/a"
    _log(f"noise FPR(含G5) = {n_pass}/{n_test} = {fpr_s}")
    with open(os.path.join(OUT_DIR, "wave3_noise_fpr.json"), "w",
              encoding="utf-8") as f:
        json.dump({"n_pass": n_pass, "n_test": n_test,
                   "fpr": round(fpr, 5) if fpr is not None else None,
                   "note": "同日重排噪声×含G5复合闸（G1用N_W3口径）"}, f,
                  ensure_ascii=False, indent=1)

    # —— 报告 ——
    _log("write report")
    L = ["# 波3 大扩容报告（2026-08-29）", "",
         f"> 预算 N_W3={N_W3}（bonf {BONO:.2e}）；闸同波1（含 G5 结构闸）。",
         f"> 噪声校准（含 G5 复合闸）：{n_pass}/{n_test} 过闸"
         f"（FPR={fpr_s}）——大扩容后漏斗可信度的实证读数。", "",
         f"## 波3 新增存活 {len(survivors)}：", ""]
    for r in sorted(survivors, key=lambda r: (r["target"],
                                              -abs(r.get("within_date_ic") or 0))):
        L.append(f"- **{r['feature']}×{r['target']}**（{r['direction']:+d}，"
                 f"IC {r['ic']:+.4f}，截面IC {r.get('within_date_ic')}，"
                 f"p {r.get('struct_emp_p')}，{r['years_same']}/5 年，"
                 f"fmΔ {r.get('fm_inc_contrast')}）")
    with open(os.path.join(OUT_DIR, "wave3_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    _log(f"[done] 存活 {len(survivors)}；noise FPR {fpr_s}；"
         f"总 {(time.time() - T0) / 60:.0f}min")


if __name__ == "__main__":
    main()
