# -*- coding: utf-8 -*-
"""信号质量 · 文献动量因子可行性回测（2026-08-28 · 20 因子方向分诊）。

用户任务：把学术界/工业界 20 个动量因子方向放到当前策略（B3 颈线法冠军）上做
可行性回测。定位：这些因子不替换策略——识别内核 C2 冻结不动——而是作为逐笔
信号的质量特征/过滤候选，进 R7 已建的 P1 判别力闸（同一套预登记纪律单源复用：
diag.quality_p1_discriminance.evaluate_feature）。

数据分诊（先于看数定死；湖=前复权日线 1997 起 + daily_basic + stock_basic）：
  可算 14 方向（日频、无前视、signal_date 收盘可知）→ 21 个特征列：
     1 残差动量   resid_mom126    CAPM 残差夏普（beta 尾随 250d / 残差窗 126d；
                                 FF3-lite 升级留待存活后再做——先测最便宜变体）
     2 信息离散度 id60/id120      sign(cumret)×(%neg−%pos)（frog-in-pan 路径质量）
     3 波动率管理 vol20/vol60     已实现波动特征（组合层仓位缩放属 P2 语义，另测）
     4 夏普动量   sharpe60/120/252  日收益 mean/std
     5 部分矩     lpm60/asym60    下行半波动 / 上下行部分矩比
    10 半方差     （日频代理 = lpm60，与 5 合并）
     9 锚定VWAP   avwap_gap       close/形态锚定 VWAP−1（日频代理：锚=最早聚集顶，
                                  即 pattern_days 回看；Σ(close×vol)/Σvol 前复权口径）
    14 行业动量   ind_mom20/60    等权行业累计收益（stock_basic.industry 110 类）
    15 行业调整   ret20/60_ex_ind 个股收益−等权行业收益
    16 中间动量   mom_12_1/mom_6_1 剔除近月（close[t−21]/close[t−N]−1）
    17 TSMOM      ret252          自身 12 月收益（连续版）
    18 UMD        umd_rank        mom_12_1 全市场截面百分位（当日全湖有效股票）
    19 MACD       macd_hist       (dif−dea)/close（前复权自算；不用 stk_factor_pro
                                 的 *_bfq 不复权口径——除权跳空污染）
    20 RSI        rsi14           Wilder 14 日（同前复权自算）
  不可算 6 方向：OFI/MLOFI/ITSM（湖无日内/订单簿数据）；TSFM/CSFM（因子动量属
    多因子策略族外，且组合层动量择时触 ADR-16 红线——择时判断权归人工）；
    主权债/大类资产动量（单一 A 股市场无跨资产池）。

诚实性锚（P0 同款）：特征全部由原始湖价格计算（非池内重算），只富化既有
64,532 笔主池 + 四折诚实池流水——信号集不重算，+400% 对照线口径不动。
先验不利证据（R7-P1）：裸动量族 ret5/ret20/ret60/pos_high60 已全灭于年段闸；
本次测的是风险剥离/路径质量/截面相对位等「提纯」变体，与已灭裸动量的冗余度
另表报告（max |ρ|）。

预登记闸（复用 P1 语义，n_tests=21 单特征无交互）：
  G1 |Spearman IC| p < 0.05/21 ∧ |Q5−Q1| ≥ 0.5pp；
  G2 2022-26 五年 hi30−lo30 同向 ≥4/5（缺样年计异向）；
  G3 wf oos2022 + oos2024 双折同向；
  存活 → 组合口径过滤测试（劣侧 30% 剔除 → outer2026 冻结口径 ann Δ）→
  filter_candidate / layering_candidate（三案教训：过滤丢收益，分层不丢；
  P2 线性分层已 VETO——存活者进待新窗口队列，不自动采纳）。

用法（仓库根 cwd）：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_momentum_factors.py
产物：logs/quality/momentum_factors.json + momentum_factors_report.md
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from diag.quality_p1_discriminance import (FOLD_GATES, MIN_DQ51, YEARS,
                                           _portfolio_ann, evaluate_feature)

LAKE = "data_lake/a_shares_daily.parquet"
IN_PARQUET = "logs/quality/trades_features.parquet"
OUT_DIR = "logs/quality"
LOAD_FROM = pd.Timestamp("2019-09-01")   # 2021 初信号 252d 回看 + 缓冲
ALPHA = 0.05
NEW_FEATURES = [
    "resid_mom126", "id60", "id120", "vol20", "vol60", "sharpe60",
    "sharpe120", "sharpe252", "lpm60", "asym60", "avwap_gap",
    "ind_mom20", "ind_mom60", "ret20_ex_ind", "ret60_ex_ind",
    "mom_12_1", "mom_6_1", "ret252", "umd_rank", "macd_hist", "rsi14",
]
EXISTING_FOR_REDUNDANCY = ["ret5", "ret20", "ret60", "pos_high60",
                           "bottom_disp", "vol5_slope", "atr_pct"]


def _t(tag, t0):
    print(f"[{_t.last:>6}] {tag} ({time.time() - t0:.0f}s)", flush=True)
_t.last = 0


def _stamp():
    _t.last = int(time.time() - T0)


T0 = time.time()


def _load_wide(cols, date_from, symbols=None):
    """湖 → date×symbol 宽表（float32）。索引列走 pyarrow 直读（pandas 拿不到）。"""
    flt = [("date", ">=", date_from.to_pydatetime())]
    if symbols is not None:
        flt.append(("symbol", "in", list(symbols)))
    tb = pq.read_table(LAKE, columns=["date", "symbol"] + cols, filters=flt)
    d = pd.to_datetime(tb.column("date").to_pandas())
    s = tb.column("symbol").to_pandas().astype(str)
    data = {c: tb.column(c).to_pandas().astype("float32") for c in cols}
    wide = {}
    for c in cols:
        ser = pd.Series(data[c].to_numpy(),
                        index=pd.MultiIndex.from_arrays([d, s]))
        wide[c] = ser.sort_index().unstack(-1).astype("float32")  # level1→columns
    return wide


def _load_long(cols, date_from, symbols):
    """湖 → long 帧（date/symbol 索引）——per-symbol 数组装配用，避免宽表
    stack 的 NaN 对齐污染（close 与 volume 的缺失模式不同）。"""
    flt = [("date", ">=", date_from.to_pydatetime()),
           ("symbol", "in", list(symbols))]
    tb = pq.read_table(LAKE, columns=["date", "symbol"] + cols, filters=flt)
    d = pd.to_datetime(tb.column("date").to_pandas())
    s = tb.column("symbol").to_pandas().astype(str)
    df = pd.DataFrame({c: tb.column(c).to_pandas().to_numpy() for c in cols},
                      index=pd.MultiIndex.from_arrays([d, s], names=["date", "symbol"]))
    return df.sort_index()


def _lookup(wide, trades_dates, trades_syms):
    """宽表 → 逐笔值（get_indexer；缺位 -1 → NaN）。"""
    ri = wide.index.get_indexer(pd.DatetimeIndex(trades_dates))
    ci = wide.columns.get_indexer(pd.Index(trades_syms))
    vals = wide.to_numpy()[np.clip(ri, 0, None), np.clip(ci, 0, None)].astype(float)
    vals[(ri < 0) | (ci < 0)] = np.nan
    return vals


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    trades = pd.read_parquet(IN_PARQUET)
    trades["signal_date"] = pd.to_datetime(trades["signal_date"])
    main_df = trades[trades["pool"] == "main"].copy()
    folds_df = {f: trades[(trades["pool"] == f) & (~trades["embargoed"])].copy()
                for f in FOLD_GATES}
    print(f"[main] {len(main_df)} 笔；folds "
          f"{ {f: len(v) for f, v in folds_df.items()} }", flush=True)

    # ============ A. 全湖宽表（close/volume；市场/行业/截面秩的原料） ============
    _t("load lake close+volume (full market, >=2019-09)", T0)
    wide = _load_wide(["close", "volume"], LOAD_FROM)
    close, volume = wide["close"], wide["volume"]
    _t(f"wide close {close.shape}", T0)

    feats = {}
    ret = close.pct_change(fill_method=None).astype("float32")

    # —— 市场等权日收益（全湖成分，均值跳过未上市 NaN）——
    mkt = ret.mean(axis=1)

    # 1 残差动量：beta 尾随 250d（协方差法），残差 126d 夏普
    _t("resid_mom126: rolling beta 250d + resid sharpe 126d", T0)
    xmu = ret.mul(mkt, axis=0).rolling(250, min_periods=200).mean()
    xmu_ = ret.rolling(250, min_periods=200).mean()
    m_mu = mkt.rolling(250, min_periods=200).mean()
    m_var = mkt.rolling(250, min_periods=200).var()
    beta = xmu.sub(xmu_.mul(m_mu, axis=0)).div(m_var, axis=0)
    resid = ret.sub(beta.mul(mkt, axis=0))
    rm_mu = resid.rolling(126, min_periods=100).mean()
    rm_sd = resid.rolling(126, min_periods=100).std()
    feats["resid_mom126"] = (rm_mu / rm_sd).astype("float32")
    del xmu, xmu_, beta, resid, rm_mu, rm_sd

    # 2 信息离散度 ID = sign(cumret_N) × (%neg − %pos)
    _t("id60/id120", T0)
    neg = (ret < 0).astype("float32")
    pos = (ret > 0).astype("float32")
    for w in (60, 120):
        pn = neg.rolling(w, min_periods=int(w * 0.8)).mean()
        pp = pos.rolling(w, min_periods=int(w * 0.8)).mean()
        cum = close / close.shift(w) - 1.0
        feats[f"id{w}"] = (np.sign(cum) * (pn - pp)).astype("float32")
        del pn, pp, cum
    del neg, pos

    # 3/4 波动与夏普动量
    _t("vol20/vol60 + sharpe60/120/252", T0)
    for w in (20, 60):
        feats[f"vol{w}"] = ret.rolling(w, min_periods=w).std().astype("float32")
    for w in (60, 120, 252):
        m = ret.rolling(w, min_periods=int(w * 0.9)).mean()
        s = ret.rolling(w, min_periods=int(w * 0.9)).std()
        feats[f"sharpe{w}"] = (m / s).astype("float32")
        del m, s

    # 5 部分矩：lpm60 = √E[min(r,0)²]；asym60 = √E[max(r,0)²]/lpm60
    _t("lpm60/asym60", T0)
    rn2 = (ret.clip(upper=0) ** 2).astype("float32")
    rp2 = (ret.clip(lower=0) ** 2).astype("float32")
    lpm = np.sqrt(rn2.rolling(60, min_periods=48).mean())
    upm = np.sqrt(rp2.rolling(60, min_periods=48).mean())
    feats["lpm60"] = lpm.astype("float32")
    feats["asym60"] = (upm / lpm).astype("float32")
    del rn2, rp2, upm, lpm

    # 16/17/18 中间动量 / TSMOM / UMD 截面秩
    _t("mom_12_1/mom_6_1/ret252/umd_rank", T0)
    feats["mom_12_1"] = (close.shift(21) / close.shift(252) - 1.0).astype("float32")
    feats["mom_6_1"] = (close.shift(21) / close.shift(126) - 1.0).astype("float32")
    feats["ret252"] = (close / close.shift(252) - 1.0).astype("float32")
    feats["umd_rank"] = feats["mom_12_1"].rank(axis=1, pct=True).astype("float32")

    # 19/20 MACD / RSI（前复权自算）
    _t("macd_hist/rsi14", T0)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    feats["macd_hist"] = ((dif - dea) / close).astype("float32")
    del ema12, ema26, dif, dea
    gain = ret.clip(lower=0)
    loss = (-ret).clip(lower=0)
    ag = gain.ewm(alpha=1.0 / 14.0, adjust=False).mean()
    al = loss.ewm(alpha=1.0 / 14.0, adjust=False).mean()
    feats["rsi14"] = (100.0 * ag / (ag + al)).astype("float32")
    del gain, loss, ag, al

    # 14/15 行业动量 / 行业调整（stock_basic.industry 等权）
    _t("industry map + ind_mom20/60", T0)
    sb = pq.read_table("data_lake/stock_basic.parquet",
                       columns=["ts_code", "industry"]).to_pandas()
    ind_map = dict(zip(sb["ts_code"].astype(str), sb["industry"].astype(str)))
    all_syms = close.columns
    ind_of = pd.Series([ind_map.get(s, np.nan) for s in all_syms],
                       index=all_syms, dtype=object)
    n_no_ind = int(ind_of.isna().sum())
    ind_names = sorted(set(ind_of.dropna().unique()))
    ind_mom = {}
    for w in (20, 60):
        ind_mom[w] = {}
        for name in ind_names:
            cols = ind_of.index[ind_of == name]
            ind_ret = ret[cols].mean(axis=1)          # 等权行业日收益
            ind_mom[w][name] = ind_ret.rolling(w, min_periods=int(w * 0.75)).sum()
        _t(f"ind_mom{w}: {len(ind_names)} 行业", T0)
    trades_ind = pd.Series([ind_map.get(s, np.nan)
                            for s in trades["symbol"]], index=trades.index)
    print(f"[industry] 湖内 {n_no_ind}/{len(all_syms)} 只无行业归属；"
          f"流水侧缺失 {int(trades_ind.isna().sum())}/{len(trades)}", flush=True)

    # 9 锚定 VWAP（形态锚 = pattern_days 回看，前复权 close×vol 加权）
    _t("avwap_gap per-trade loop", T0)
    t_syms = sorted(trades["symbol"].unique())
    long_cv = _load_long(["close", "volume"], LOAD_FROM, symbols=t_syms)
    arrays = {}
    for sym, sub in long_cv.groupby(level="symbol"):
        sub = sub.droplevel("symbol")
        ok = sub["close"].notna() & sub["volume"].notna()
        sub = sub[ok]
        arrays[sym] = (sub.index, sub["close"].to_numpy(dtype=float),
                       sub["volume"].to_numpy(dtype=float))
    del long_cv
    avwap = np.full(len(trades), np.nan)
    t_dates = trades["signal_date"].to_numpy()
    for k, (sym, sd, pd_) in enumerate(zip(trades["symbol"], t_dates,
                                           trades["pattern_days"])):
        arr = arrays.get(sym)
        if arr is None:
            continue
        idx, c, v = arr
        p = idx.searchsorted(pd.Timestamp(sd), side="right") - 1
        if p < 0:
            continue
        w = int(pd_) if pd_ == pd_ and pd_ > 0 else 60   # pattern_days 缺失→60d 锚
        s = max(0, p - w)
        vv = v[s:p + 1]
        sv = vv.sum()
        if sv > 0 and not np.isnan(c[p]):
            avwap[k] = c[p] / float((c[s:p + 1] * vv).sum() / sv) - 1.0
    del arrays

    # ============ B. 逐笔装配 ============
    _t("assemble per-trade features", T0)
    out = pd.DataFrame(index=trades.index)
    td, ts = trades["signal_date"], trades["symbol"]
    for name in ["resid_mom126", "id60", "id120", "vol20", "vol60", "sharpe60",
                 "sharpe120", "sharpe252", "lpm60", "asym60", "mom_12_1",
                 "mom_6_1", "ret252", "umd_rank", "macd_hist", "rsi14"]:
        out[name] = _lookup(feats[name], td, ts)
    out["avwap_gap"] = avwap
    ret20 = _lookup((close / close.shift(20) - 1.0).astype("float32"), td, ts)
    ret60 = _lookup((close / close.shift(60) - 1.0).astype("float32"), td, ts)
    for w, ret_v in ((20, ret20), (60, ret60)):
        col = np.full(len(trades), np.nan)
        for name, ser in ind_mom[w].items():
            mask = (trades_ind == name).to_numpy()
            if mask.any():
                col[mask] = ser.reindex(td[mask]).to_numpy()
        out[f"ret{w}_ex_ind"] = ret_v - col
        out[f"ind_mom{w}"] = col

    # ============ C. 四闸评估（复用 P1 单源） ============
    _t("gates", T0)
    n_tests = len(NEW_FEATURES)
    bonf = ALPHA / n_tests
    print(f"[prereg] n_tests={n_tests} bonf_alpha={bonf:.2e} "
          f"|dq51|>={MIN_DQ51}pp 年段闸≥4/5 wf双折同向", flush=True)
    for frame in [main_df] + list(folds_df.values()):
        for c in NEW_FEATURES:
            frame[c] = out.loc[frame.index, c].to_numpy()
    results = [evaluate_feature(main_df, folds_df, c, ALPHA, n_tests)
               for c in NEW_FEATURES]
    survivors = [r for r in results if r.get("survivor")]
    print(f"[stageB] 存活 {len(survivors)}："
          f"{[r['feature'] for r in survivors]}", flush=True)

    # 冗余度：新特征 × 既有特征（含 R7 已灭裸动量族）max |Spearman ρ|
    _t("redundancy vs existing", T0)
    redundancy = {}
    for c in NEW_FEATURES:
        s = main_df[c].dropna()
        if len(s) < 500:
            redundancy[c] = None
            continue
        best, best_v = None, 0.0
        for e in EXISTING_FOR_REDUNDANCY:
            se = main_df.loc[s.index, e].dropna()
            common = s.index.intersection(se.index)
            if len(common) < 500:
                continue
            rho = main_df.loc[common, c].corr(main_df.loc[common, e],
                                              method="spearman")
            if rho == rho and abs(rho) > abs(best_v):
                best, best_v = e, rho
        redundancy[c] = {"max_abs_rho": round(float(best_v), 3),
                         "vs": best} if best else None

    # ============ D. 存活者过滤测试（P1 同款分类） ============
    _t("filter test for survivors", T0)
    from backtest.models import PositionModel
    from discovery.split import Segment, holdout_split
    split = holdout_split()
    full_seg = Segment("full", __import__("datetime").date(2021, 1, 1),
                       __import__("datetime").date(2026, 12, 31))
    _dates = pq.read_table(LAKE, columns=["date"]).column("date").to_pandas()
    udates = pd.DatetimeIndex(sorted(_dates.unique()))
    udates = udates[udates >= pd.Timestamp("2021-01-01")]
    pm_freeze = PositionModel(capital=1_000_000, lot_size=100, min_fee=5.0,
                              max_positions=4, pos_cap=0.075,
                              freeze_pending=True)
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
            "signal_mean_uplift_pp": round(float(
                kept["avg_pnl_pct"].mean() - main_df["avg_pnl_pct"].mean()), 3),
            "outer": _portfolio_ann(fo, split.outer, udates, pm_freeze),
            "full": _portfolio_ann(ff, full_seg, udates, pm_freeze),
        }
        gain = sv["filter_test"]["outer"]["ann"] - ann_base_outer["ann"]
        sv["classification"] = ("filter_candidate" if gain > 0
                                else "layering_candidate")
        print(f"[class] {c}: 外层 ann Δ{gain:+.3f} → {sv['classification']}",
              flush=True)

    # ============ E. 产物 ============
    _t("write outputs", T0)
    report = {
        "preregistration": {
            "gates": ["G1 |IC| Bonferroni p<{:.2e} & |Q5-Q1|>=0.5pp".format(bonf),
                      "G2 >=4/5 年(2022-26) hi30-lo30 同向（缺样年计异向）",
                      "G3 wf oos2022 & oos2024 双折同向",
                      "存活→过滤测试分类（P1 同款）"],
            "n_tests": n_tests, "features": NEW_FEATURES,
            "baseline_ann": {"outer2026_freeze": ann_base_outer,
                             "full2021_26_freeze": ann_base_full},
        },
        "feasibility_triage": {
            "computable_14_directions": {
                "residual_mom": ["resid_mom126"],
                "info_discreteness": ["id60", "id120"],
                "vol_managed_feature": ["vol20", "vol60"],
                "sharpe_mom": ["sharpe60", "sharpe120", "sharpe252"],
                "partial_moment": ["lpm60", "asym60"],
                "semivariance_daily_proxy": ["lpm60"],
                "anchored_vwap_daily_proxy": ["avwap_gap"],
                "industry_mom": ["ind_mom20", "ind_mom60"],
                "industry_adjusted": ["ret20_ex_ind", "ret60_ex_ind"],
                "intermediate_mom": ["mom_12_1", "mom_6_1"],
                "tsmom": ["ret252"],
                "umd_rank": ["umd_rank"],
                "macd": ["macd_hist"], "rsi": ["rsi14"],
            },
            "infeasible_6_directions": {
                "ofi": "湖无订单簿", "mlofi": "湖无订单簿",
                "itsm_intraday": "湖无日内数据",
                "tsfm_factor_mom": "多因子策略族外+组合层择时触 ADR-16",
                "csfm_factor_mom": "同上",
                "bond_asset_class_mom": "单一 A 股市场无跨资产池",
            },
            "caveats": [
                "行业归属=stock_basic 今日截面（历史变更未追踪，可行性级近似）",
                "anchored VWAP=日频代理（Σclose×vol/Σvol），非分时 VWAP",
                "stk_factor_pro 的 MACD/RSI 为不复权口径被弃用，全部前复权自算",
                "市场/行业等权收益=湖成分（5799 只，含退市？以湖为准）",
            ],
        },
        "survivors": survivors,
        "redundancy_vs_existing": redundancy,
        "all_features": results,
        "industry_coverage": {"lake_symbols_no_industry": n_no_ind,
                              "lake_symbols": int(len(all_syms)),
                              "trades_no_industry": int(trades_ind.isna().sum())},
        "generated_at": pd.Timestamp.now().isoformat(),
    }
    with open(os.path.join(OUT_DIR, "momentum_factors.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=str)

    # —— md 人读版 ——
    L, ap = [], None
    ap = L.append
    ap("# 文献动量因子可行性回测（20 方向 · 2026-08-28）")
    ap("")
    ap("> 框架：R7-P1 同闸单源复用（Bonferroni n=21、年段 ≥4/5、wf 双折同向）；"
       "特征由原始湖前复权价计算，富化既有 64,532 笔主池 + 诚实池流水，"
       "对照线口径不动。")
    ap(f"> 基线复核：outer2026 冻结口径 ann {ann_base_outer['ann']:+.1%} / "
       f"taken {ann_base_outer['n_taken']}；全期 ann "
       f"{ann_base_full['ann']:+.1%} / taken {ann_base_full['n_taken']}。")
    ap("")
    ap("## 一、20 方向分诊")
    ap("")
    ap("| 方向 | 数据支撑 | 判定 | 特征列 |")
    ap("|---|---|---|---|")
    ap("| 1 残差动量 | 日线+等权市场 | ✅ 日频 CAPM 版 | resid_mom126 |")
    ap("| 2 信息离散度 | 日线 | ✅ | id60/id120 |")
    ap("| 3 波动率管理 | 日线 | ✅ 特征面（组合缩放属 P2 另测） | vol20/vol60 |")
    ap("| 4 夏普动量 | 日线 | ✅ | sharpe60/120/252 |")
    ap("| 5 部分矩 | 日线 | ✅ | lpm60/asym60 |")
    ap("| 6 OFI / 7 MLOFI | 需订单簿 | ❌ 湖无数据 | — |")
    ap("| 8 日内时序动量 | 需分时 | ❌ 湖无数据 | — |")
    ap("| 9 锚定VWAP | 日线 | ⚠️ 日频代理（形态锚） | avwap_gap |")
    ap("| 10 已实现半方差 | 日线 | ⚠️ 日频代理=下行半波动 | lpm60 |")
    ap("| 11 TSFM / 12 CSFM | 因子组合 | ❌ 族外 + ADR-16 择时红线 | — |")
    ap("| 13 主权债动量 | 跨资产 | ❌ 无资产池 | — |")
    ap("| 14 行业动量 | stock_basic 行业 | ✅ 等权行业 | ind_mom20/60 |")
    ap("| 15 行业调整 | 同上 | ✅ | ret20/60_ex_ind |")
    ap("| 16 中间动量 | 日线 | ✅ | mom_12_1/mom_6_1 |")
    ap("| 17 TSMOM | 日线 | ✅ | ret252 |")
    ap("| 18 UMD | 日线全截面 | ✅ | umd_rank |")
    ap("| 19 MACD / 20 RSI | 日线 | ✅ 前复权自算 | macd_hist/rsi14 |")
    ap("")
    ap(f"**存活 {len(survivors)} 个**：" +
       ("、".join(f"`{s['feature']}`" for s in survivors) if survivors else "（无）"))
    ap("")
    for s in survivors:
        ft = s.get("filter_test", {})
        ap(f"### {s['feature']}（方向 {'高好' if s['direction'] > 0 else '低好'}，"
           f"IC={s['ic']:+.4f}，ΔQ5−Q1={s['dq51']:+.2f}pp）")
        ap("")
        ap(f"- 五分位均值：{s['q_means']}")
        ap(f"- 年段对比：{s['year_contrasts']} → 同向 {s['years_same']}/5；"
           f"wf 折：{s['fold_contrasts']}")
        ap(f"- 过滤测试（剔 {ft.get('drop')}，外层剔 "
           f"{ft.get('n_dropped_outer')} 笔）：信号均值 "
           f"{ft.get('signal_mean_uplift_pp', 0):+.2f}pp；outer ann "
           f"{ft['outer']['ann']:+.1%}（Δ"
           f"{ft['outer']['ann'] - ann_base_outer['ann']:+.3f}）；全期 "
           f"{ft['full']['ann']:+.1%}")
        ap(f"- **分类：{s['classification']}**")
        ap("")
    ap("## 二、全特征四闸明细")
    ap("")
    rows = [{"feature": r["feature"], "n": r.get("n"), "IC": r.get("ic"),
             "ΔQ5−Q1": r.get("dq51"), "G1": r.get("gate_G1", False),
             "年同向": f"{r.get('years_same', 0)}/5" if r.get("year_contrasts") else "",
             "G3双折": "过" if r.get("gate_G3") else str(r.get("fold_contrasts")),
             "存活": r.get("survivor", False)} for r in results]
    ap(pd.DataFrame(rows).to_string(index=False))
    ap("")
    ap("## 三、与既有特征的冗余度（max |ρ|，含 R7 已灭裸动量族）")
    ap("")
    ap(pd.DataFrame([
        {"feature": c, "max_abs_rho": (v or {}).get("max_abs_rho"),
         "vs": (v or {}).get("vs")} for c, v in redundancy.items()]
    ).to_string(index=False))
    ap("")
    with open(os.path.join(OUT_DIR, "momentum_factors_report.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[done] {OUT_DIR}/momentum_factors.json + momentum_factors_report.md "
          f"(总用时 {(time.time() - T0) / 60:.0f}min)", flush=True)


if __name__ == "__main__":
    main()
