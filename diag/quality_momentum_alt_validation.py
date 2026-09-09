# -*- coding: utf-8 -*-
"""信号质量 · 存活者快速替代验证（2026-08-28 · 方案②③④合并发射）。

用户裁决：不等 fresh_window（9 月中下旬太慢），先跑三条替代验证轴。
对象 = batch1 四存活者（方向预登记死于此，先于本脚本任何读数）：
    id60=+1（高好）  lpm60=−1  ret20_ex_ind=−1  mom_6_1=−1

预登记判定规则（先于看数写死）：
  ② 全市场池复验（trades_features_fullmkt，R7a 建于本批特征筛选之前，未参与
     任何 batch1/batch2 假设形成）：
     判定 A（全池）: fullmkt 全池 hi30−lo30 与 batch1 方向同号；
     判定 B（增量切片）: 主池外增量笔（symbol ∉ 主池 1168 只——退市/被今日截面
       排除的股票，batch1 物理上看不到）hi30−lo30 与方向同号；
     复现 = A ∧ B；仅 A = 部分复现（疑主池污染）；A∧¬B 或 ¬A = 未复现（杀）。
     逐年对比只报告不裁决；对比口径 = P1._contrast 单源。
  ③ 噪声对照漏斗校准：200 个结构保持噪声列（4 特征 × 50 次同 signal_date 内
     随机重排——保留边际分布与同日结构，破坏特征-pnl 链接），过与真筛完全相同
     的四闸（evaluate_feature 单源，n_tests=26 同操作点）。读数 = 经验假阳性率：
     0/200 过闸 = 漏斗特异性实证；>10/200 = 存活者可信度按比例打折。
  ④ 置换检验：每存活特征 1000 次同日重排 → 秩相关 null 分布 → 双侧经验 p
     （秩相关=秩上 Pearson，预变换后单次 corr ~1ms）。

用法（仓库根 cwd）：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_momentum_alt_validation.py
产物：logs/quality/momentum_alt_validation.json + momentum_alt_validation_report.md
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from diag.quality_p1_discriminance import FOLD_GATES, _contrast, evaluate_feature
from diag.quality_momentum_batch2 import _lk, _matrix

LAKE = "data_lake/a_shares_daily.parquet"
IN_MAIN = "logs/quality/trades_features.parquet"
IN_FULLMKT = "logs/quality/trades_features_fullmkt.parquet"
OUT_DIR = "logs/quality"
LOAD_FROM = pd.Timestamp("2020-03-01")     # 60/126 日窗 + 缓冲（无 252 日需求）
DIRECTIONS = {"id60": 1, "lpm60": -1, "ret20_ex_ind": -1, "mom_6_1": -1}
ALPHA = 0.05
N_TESTS = 26
N_NOISE_REPS = 50
N_PERM = 1000
RNG_SEED = 20260828
T0 = time.time()


def _t(tag):
    print(f"[{time.time() - T0:>6.0f}s] {tag}", flush=True)


def _permute_within(df, col, rng):
    """同 signal_date 内随机重排该列（保边际+同日结构，断 pnl 链接）。
    返回重排后的 numpy 数组。"""
    v = df[col].to_numpy(dtype=float, copy=True)
    ok = np.isfinite(v)
    d = df["signal_date"].to_numpy()
    dcode = pd.factorize(d)[0]
    for g in np.unique(dcode):
        m = (dcode == g) & ok
        k = m.sum()
        if k > 1:
            v[m] = v[m][rng.permutation(k)]
    return v


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rng = np.random.default_rng(RNG_SEED)

    trades = pd.read_parquet(IN_MAIN)
    trades["signal_date"] = pd.to_datetime(trades["signal_date"])
    main_df = trades[trades["pool"] == "main"].copy()
    folds_df = {f: trades[(trades["pool"] == f) & (~trades["embargoed"])].copy()
                for f in FOLD_GATES}
    fm = pd.read_parquet(IN_FULLMKT)
    fm["signal_date"] = pd.to_datetime(fm["signal_date"])
    main_syms = set(main_df["symbol"])
    fm_inc = fm[~fm["symbol"].isin(main_syms)].copy()   # 增量切片：主池外股票
    print(f"[main] {len(main_df)} 笔；fullmkt {len(fm)} 笔；"
          f"增量切片 {len(fm_inc)} 笔（{fm['symbol'].nunique() - len(main_syms & set(fm['symbol']))} 只主池外股票）",
          flush=True)

    # ============ 特征计算（主池 + fullmkt 两套流水共用一套宽表） ============
    _t(f"load close from {LOAD_FROM.date()}")
    close = _matrix(LAKE, ["close"], LOAD_FROM)["close"]
    ret = close.pct_change(fill_method=None).astype("float32")
    neg60 = (ret < 0).astype("float32")
    pos60 = (ret > 0).astype("float32")
    pn = neg60.rolling(60, min_periods=48).mean()
    pp = pos60.rolling(60, min_periods=48).mean()
    cum60 = close / close.shift(60) - 1.0
    id60 = (np.sign(cum60) * (pn - pp)).astype("float32")
    del neg60, pos60, pn, pp, cum60
    rn2 = (ret.clip(upper=0) ** 2).astype("float32")
    lpm60 = np.sqrt(rn2.rolling(60, min_periods=48).mean()).astype("float32")
    del rn2
    mom_6_1 = (close.shift(21) / close.shift(126) - 1.0).astype("float32")

    _t("industry ret20 (等权)")
    sb = pq.read_table("data_lake/stock_basic.parquet",
                       columns=["ts_code", "industry"]).to_pandas()
    ind_map = dict(zip(sb["ts_code"].astype(str), sb["industry"].astype(str)))
    ind_of = pd.Series([ind_map.get(s, np.nan) for s in close.columns],
                       index=close.columns, dtype=object)
    ret20 = (close / close.shift(20) - 1.0).astype("float32")
    ind_r20 = {}
    for name in sorted(set(ind_of.dropna().unique())):
        cols = ind_of.index[ind_of == name]
        ind_r20[name] = ret[cols].mean(axis=1).rolling(20, min_periods=15).sum()
    ret20_ex_ind_wide = pd.DataFrame(np.nan, index=close.index, columns=close.columns,
                                     dtype="float32")
    for name, s in ind_r20.items():
        cols = list(ind_of.index[ind_of == name])
        ret20_ex_ind_wide[cols] = ret20[cols].sub(s, axis=0).astype("float32")

    def _attach(df):
        td, ts = df["signal_date"], df["symbol"]
        df["id60"] = _lk(id60, td, ts)
        df["lpm60"] = _lk(lpm60, td, ts)
        df["mom_6_1"] = _lk(mom_6_1, td, ts)
        df["ret20_ex_ind"] = _lk(ret20_ex_ind_wide, td, ts)
        return df

    main_df = _attach(main_df)
    for f in folds_df:
        folds_df[f] = _attach(folds_df[f])
    fm = _attach(fm)
    fm_inc = fm[~fm["symbol"].isin(main_syms)].copy()
    _t("features attached (main/folds/fullmkt)")

    # ============ ② 全市场池复验 ============
    _t("part2: fullmkt reproduction")
    rep2 = {}
    for feat, d in DIRECTIONS.items():
        c_full = _contrast(fm, feat)
        c_inc = _contrast(fm_inc, feat)
        same_full = c_full is not None and np.sign(c_full) == d
        same_inc = c_inc is not None and np.sign(c_inc) == d
        by_year_full = {int(y): _contrast(fm[fm["signal_date"].dt.year == y], feat)
                        for y in range(2022, 2027)}
        by_year_inc = {int(y): _contrast(fm_inc[fm_inc["signal_date"].dt.year == y],
                                         feat) for y in range(2022, 2027)}
        verdict = ("复现" if (same_full and same_inc)
                   else "部分复现(疑主池污染)" if same_full else "未复现(杀)")
        n_cov = {"fullmkt": int(fm[feat].notna().sum()),
                 "inc": int(fm_inc[feat].notna().sum())}
        rep2[feat] = {"direction": d,
                      "contrast_fullmkt": None if c_full is None else round(c_full, 2),
                      "contrast_incremental": None if c_inc is None else round(c_inc, 2),
                      "same_fullmkt": bool(same_full),
                      "same_incremental": bool(same_inc),
                      "by_year_fullmkt": {k: None if v is None else round(v, 2)
                                          for k, v in by_year_full.items()},
                      "by_year_incremental": {k: None if v is None else round(v, 2)
                                              for k, v in by_year_inc.items()},
                      "coverage": n_cov,
                      "verdict": verdict}
        fmt = lambda x: "n/a" if x is None else f"{x:+.2f}"
        print(f"[②] {feat}: 全池 {fmt(c_full)} / 增量 {fmt(c_inc)} "
              f"(方向 {d:+d}) → {verdict}", flush=True)

    # ============ ③ 噪声对照漏斗校准 ============
    _t(f"part3: {len(DIRECTIONS) * N_NOISE_REPS} noise features through funnel")
    n_pass, n_tested = 0, 0
    noise_pass_cols = []
    for rep_i in range(N_NOISE_REPS):
        for feat in DIRECTIONS:
            col = f"__noise_{feat}_{rep_i}"
            main_df[col] = _permute_within(main_df, feat, rng)
            for f in folds_df:
                folds_df[f][col] = _permute_within(folds_df[f], feat, rng)
            r = evaluate_feature(main_df, folds_df, col, ALPHA, N_TESTS)
            n_tested += 1
            if r.get("survivor"):
                n_pass += 1
                noise_pass_cols.append({"col": col, "ic": r.get("ic"),
                                        "years": r.get("years_same")})
            main_df.drop(columns=[col], inplace=True)
            for f in folds_df:
                folds_df[f].drop(columns=[col], inplace=True)
        if (rep_i + 1) % 10 == 0:
            _t(f"noise rep {rep_i + 1}/{N_NOISE_REPS}: pass {n_pass}/{n_tested}")
    fpr = n_pass / n_tested if n_tested else None
    print(f"[③] 噪声过闸 {n_pass}/{n_tested} (FPR={fpr:.3%})" if fpr is not None
          else "[③] 无有效噪声列", flush=True)

    # ============ ④ 置换检验（经验 p） ============
    _t(f"part4: permutation test x{N_PERM}")
    rep4 = {}
    dates_main = main_df["signal_date"].to_numpy()
    dcode = pd.factorize(dates_main)[0]
    pnl_rank = main_df["avg_pnl_pct"].rank().to_numpy()
    for feat in DIRECTIONS:
        v = main_df[feat].to_numpy(dtype=float)
        ok = np.isfinite(v)
        fr = pd.Series(v[ok]).rank().to_numpy()
        pr = pnl_rank[ok]
        dc = dcode[ok]
        obs = float(np.corrcoef(fr, pr)[0, 1])
        # 组边界（排序后 split 点）加速组内重排
        order = np.argsort(dc, kind="stable")
        fr_s, dc_s = fr[order], dc[order]
        bounds = np.flatnonzero(np.diff(dc_s)) + 1
        groups = np.split(fr_s, bounds)
        cnt = 0
        for _ in range(N_PERM):
            g2 = [g[rng.permutation(len(g))] for g in groups]
            null = np.concatenate(g2)
            if abs(float(np.corrcoef(null, pr[order])[0, 1])) >= abs(obs):
                cnt += 1
        emp_p = (1 + cnt) / (N_PERM + 1)
        rep4[feat] = {"observed_ic": round(obs, 4),
                      "n": int(ok.sum()),
                      "perm_hits_ge_abs_obs": cnt,
                      "emp_p_two_sided": round(emp_p, 4)}
        print(f"[④] {feat}: obs IC {obs:+.4f}, null≥|obs| {cnt}/{N_PERM} "
              f"→ 经验 p={emp_p:.4f}", flush=True)

    # ============ 产物 ============
    _t("write outputs")
    out = {
        "preregistration": {
            "directions": DIRECTIONS,
            "part2_rule": "复现=全池同向 ∧ 增量切片同向；仅全池=部分复现；否则杀",
            "part3_rule": f"{len(DIRECTIONS) * N_NOISE_REPS} 同日重排噪声列过同一四闸"
                          "(n_tests=26)；FPR≤0.5%=漏斗特异性实证",
            "part4_rule": f"同日重排 x{N_PERM} 秩相关双侧经验 p",
            "data_basis": ("fullmkt 特征库建于 2026-08-27 R7a（早于 08-28 "
                           "batch1/batch2 特征筛选），未参与假设形成"),
        },
        "part2_fullmkt_reproduction": rep2,
        "part3_noise_funnel_fpr": {"pass": n_pass, "tested": n_tested,
                                   "fpr": round(fpr, 5) if fpr is not None else None,
                                   "passing_cols": noise_pass_cols},
        "part4_permutation": rep4,
        "generated_at": pd.Timestamp.now().isoformat(),
    }
    with open(os.path.join(OUT_DIR, "momentum_alt_validation.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)

    L, ap = [], None
    ap = L.append
    ap("# 存活者快速替代验证（②全市场池 ③噪声对照 ④置换检验 · 2026-08-28）")
    ap("")
    ap("> 对象=batch1 四存活者；判定规则先于看数预登记（见脚本头/json）。"
       "fullmkt 库建于 R7a（08-27），早于本批特征筛选。")
    ap("")
    ap("## ② 全市场池复验（181,072 笔全池 / 116,540 笔主池外增量）")
    ap("")
    ap("| 特征 | 方向 | 全池对比 | 增量切片对比 | 年段(全池) | 裁决 |")
    ap("|---|---|---|---|---|---|")
    for feat, r in rep2.items():
        ap(f"| {feat} | {r['direction']:+d} | {r['contrast_fullmkt']:+} | "
           f"{r['contrast_incremental']:+} | {r['by_year_fullmkt']} | "
           f"**{r['verdict']}** |")
    ap("")
    ap("## ③ 噪声对照漏斗校准")
    ap("")
    ap(f"- {n_tested} 个同日重排噪声列过同一四闸：**{n_pass} 个存活"
       f"（经验 FPR={fpr:.2%}）**")
    if noise_pass_cols:
        ap(f"- 过闸噪声列：{noise_pass_cols}")
    ap("")
    ap("## ④ 置换检验（同日重排空分布，双侧经验 p）")
    ap("")
    ap("| 特征 | obs IC | null≥\\|obs\\| | 经验 p |")
    ap("|---|---|---|---|")
    for feat, r in rep4.items():
        ap(f"| {feat} | {r['observed_ic']:+.4f} | "
           f"{r['perm_hits_ge_abs_obs']}/{N_PERM} | {r['emp_p_two_sided']:.4f} |")
    ap("")
    with open(os.path.join(OUT_DIR, "momentum_alt_validation_report.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[done] {OUT_DIR}/momentum_alt_validation.json + "
          f"momentum_alt_validation_report.md (总用时 "
          f"{(time.time() - T0) / 60:.0f}min)", flush=True)


if __name__ == "__main__":
    main()
