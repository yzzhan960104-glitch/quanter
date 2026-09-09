# -*- coding: utf-8 -*-
"""信号质量 · 双轴因子动物园循环（2026-08-29 夜间自主探索 · 用户指令）。

用户指令：一晚上时间，尽所能找最合适颈线法的调优因子（吞吐量+信号质量两轴），
不要停止。边界（继承战役纪律，夜间自主不豁免）：
  - 本循环只做「发现+筛选+登记」，不采纳不部署——采纳裁决归用户晨会；
  - C2 冻结：不碰识别内核；ADR-16：无池子侧 regime 特征进决策（本循环全部
    个股侧 at_signal 特征）；
  - 存活者定义=登记册条目，不是策略变更。

══════════ 预登记（2026-08-29 写死于运行前）══════════
目标双轴（各自独立闸）：
  Y_pnl = avg_pnl_pct（单笔盈亏，质量轴）
  Y_occ = wait_days + holding_bars（资金占用天数，吞吐轴——抢槽优先级的
          能力前提：同日内分辨谁占用短）
四闸（语义复制自 P1 evaluate_feature + 本 session 新增 G5 结构闸）：
  G1 |Spearman IC| p < 0.05/N_TOTAL ∧ |Q5−Q1| ≥ 0.5（pnl:pp / occ:天）
  G2 2022-26 五年 hi30−lo30 同向 ≥4/5（缺样年计异向）
  G3 wf1(oos2022) + wf2(oos2024) 双折同向
  G5 结构闸（08-28 教训）：同日置换经验 p < 0.05（B=400，保日级结构断截面
     链接）∧ 日内截面 IC 与总 IC 同号——防状态代理混入
N_TOTAL = 180（波1 ~62 列×2 轴 + 既有特征 occ 轴 18 + 交互 ≤20 + 波2 守卫族
  ~10，一次性预扣，比动态计数更保守）。
排除项（已测）：动量族 ret5/20/60/mom_6_1/id60/夏普/UMD/TSMOM/MACD/RSI/
  12月窗族/残差动量族（batch1/2 已裁决，不重测 pnl 轴；occ 轴按 R7d 扩展重测）。
波 2（守卫启用）：moneyflow 主力净流入 / margin 融资余额变化 / cyq 筹码获利盘
  ——schema 不符或主池覆盖 <60% 则整族跳过（日志留痕，不中断）。
鲁棒附加（登记册字段，非闸）：存活者自动跑 fullmkt 主池外增量切片方向复现。
收敛/预算：族谱有限（~36 族），跑完全部族+交互+复现即写终报退出；--hours N
  为硬预算（默认 10h）；逐族 try/except 失败不中断（「不要停止」）。

用法（仓库根 cwd）：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_zoo_loop.py [--hours 10] [--smoke]
产物：logs/quality/factor_zoo/{progress.md, registry.jsonl, survivors.json, final_report.md}
"""
import argparse
import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from diag.quality_momentum_batch2 import _lk, _matrix

LAKE = "data_lake/a_shares_daily.parquet"
DAILY_BASIC = "data_lake/daily_basic.parquet"
IN_MAIN = "logs/quality/trades_features.parquet"
IN_FULLMKT = "logs/quality/trades_features_fullmkt.parquet"
OUT_DIR = "logs/quality/factor_zoo"
LOAD_FROM = pd.Timestamp("2019-06-01")
ALPHA = 0.05
N_TOTAL = 180
B_PERM = 400
YEARS = [2022, 2023, 2024, 2025, 2026]
FOLD_KEYS = ["wf1_2020_21", "wf2_2022_23"]
MIN_DQ = 0.5
EXISTING_AT_SIGNAL = ["h_atr", "rr_id", "touches", "n_tops", "bottom_disp",
                      "pattern_days", "neckline_span", "suppression", "ret5",
                      "ret20", "ret60", "pos_high60", "breakout_ret", "atr_pct",
                      "bvr", "vol5_slope", "pv_corr10", "same_day_n"]
RNG = np.random.default_rng(20260829)
T0 = time.time()
DEADLINE = None


def _log(msg):
    print(f"[{time.time() - T0:>6.0f}s] {msg}", flush=True)


def _progress(line):
    with open(os.path.join(OUT_DIR, "progress.md"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ============================================================ 闸（P1 语义 + G5）
def _contrast(df, col, target, min_n=20):
    s = df[col].dropna()
    if len(s) < 200:
        return None
    lo_t, hi_t = s.quantile([0.3, 0.7])
    lo = df.loc[df[col] <= lo_t, target]
    hi = df.loc[df[col] >= hi_t, target]
    if len(lo) < min_n or len(hi) < min_n:
        return None
    return float(hi.mean() - lo.mean())


def _quintile(df, col, target):
    s = df[col].dropna()
    if len(s) < 500:
        return None, None
    try:
        b = pd.qcut(df[col], 5, duplicates="drop")
    except ValueError:
        b = pd.qcut(df[col].rank(method="first"), 5, duplicates="drop")
    g = df.groupby(b, observed=True)[target].mean()
    return [round(v, 2) for v in g.tolist()], float(g.iloc[-1] - g.iloc[0])


def _structure(main_df, col, target, rng):
    """G5：同日置换经验 p + 日内截面 IC。

    null 语义（与 08-28 结构检验一致）：特征取**全局秩**后同日内重排——
    日级（行情状态）成分原样保留、截面链接被打碎；obs 必须超过这个空分布
    才算有选股信息。wic 另算（日内去均值池化相关）。"""
    v = main_df[col].to_numpy(dtype=float)
    y = main_df[target].to_numpy(dtype=float)
    ok = np.isfinite(v) & np.isfinite(y)
    if ok.sum() < 500:
        return None, None
    dcode = pd.factorize(main_df["signal_date"].to_numpy()[ok])[0]
    fr = pd.Series(v[ok]).rank().to_numpy()            # 全局秩
    yr = pd.Series(y[ok]).rank().to_numpy()            # 全局秩
    obs = float(np.corrcoef(fr, yr)[0, 1])
    xr = pd.Series(v[ok]).groupby(dcode).rank(pct=True).to_numpy()
    yrr = pd.Series(y[ok]).groupby(dcode).rank(pct=True).to_numpy()
    dfz = pd.DataFrame({"g": dcode, "x": xr, "y": yrr})
    z = dfz.groupby("g")[["x", "y"]].transform(
        lambda s: (s - s.mean()) / (s.std(ddof=0) if s.std(ddof=0) > 0 else 1.0))
    wic = float(np.corrcoef(z["x"], z["y"])[0, 1])
    order = np.argsort(dcode, kind="stable")
    fr_s, dc_s, yr_s = fr[order], dcode[order], yr[order]
    groups = np.split(fr_s, np.flatnonzero(np.diff(dc_s)) + 1)
    cnt = 0
    for _ in range(B_PERM):
        null = np.concatenate([g[rng.permutation(len(g))] for g in groups])
        if abs(float(np.corrcoef(null, yr_s)[0, 1])) >= abs(obs):
            cnt += 1
    return (1 + cnt) / (B_PERM + 1), wic


def evaluate(col, target, values, main_df, folds, rng, bonf):
    """单列×单轴全闸。values: 与 trades 等长的数组（先挂到 frame 再评）。"""
    main_df[col] = values["main"]
    for f in FOLD_KEYS:
        folds[f][col] = values[f]
    s = main_df[col].dropna()
    res = {"feature": col, "target": target, "n": int(len(s))}
    if len(s) < 500:
        res.update({"gate_G1": False, "why": "n<500", "survivor": False})
        return res
    from scipy import stats
    ic, p = stats.spearmanr(s, main_df.loc[s.index, target])
    q, dq = _quintile(main_df, col, target)
    res["ic"] = round(float(ic), 4)
    res["dq51"] = round(dq, 3) if dq is not None else None
    res["q_means"] = q
    g1 = (p < bonf) and (dq is not None and abs(dq) >= MIN_DQ)
    res["gate_G1"] = bool(g1)
    d = 1 if ic >= 0 else -1
    res["direction"] = d
    same, yr_detail = 0, {}
    for y_ in YEARS:
        c = _contrast(main_df[main_df["signal_date"].dt.year == y_], col, target)
        yr_detail[y_] = None if c is None else round(c, 2)
        if c is not None and np.sign(c) == d:
            same += 1
    res["year_contrasts"] = yr_detail
    res["years_same"] = same
    res["gate_G2"] = same >= 4
    g3, fold_detail = True, {}
    for f in FOLD_KEYS:
        c = _contrast(folds[f], col, target)
        fold_detail[f] = None if c is None else round(c, 2)
        if c is None or np.sign(c) != d:
            g3 = False
    res["fold_contrasts"] = fold_detail
    res["gate_G3"] = bool(g3)
    emp_p, wic = _structure(main_df, col, target, rng)
    res["struct_emp_p"] = None if emp_p is None else round(emp_p, 4)
    res["within_date_ic"] = None if wic is None or wic != wic else round(wic, 4)
    g5 = (emp_p is not None and emp_p < 0.05 and wic is not None and wic == wic
          and np.sign(wic) == d)
    res["gate_G5"] = bool(g5)
    res["survivor"] = bool(g1 and res["gate_G2"] and g3 and g5)
    return res


# ============================================================ 数据底座
def build_bases():
    trades = pd.read_parquet(IN_MAIN)
    trades["signal_date"] = pd.to_datetime(trades["signal_date"])
    main_df = trades[trades["pool"] == "main"].copy().reset_index(drop=True)
    folds = {f: trades[(trades["pool"] == f) & (~trades["embargoed"])].copy()
             .reset_index(drop=True) for f in FOLD_KEYS}
    fm = pd.read_parquet(IN_FULLMKT)
    fm["signal_date"] = pd.to_datetime(fm["signal_date"])
    main_syms = set(main_df["symbol"])
    fm_inc = fm[~fm["symbol"].isin(main_syms)].copy().reset_index(drop=True)
    for df in [main_df] + list(folds.values()) + [fm_inc]:
        df["occ"] = pd.to_numeric(df["wait_days"], errors="coerce") + \
            pd.to_numeric(df["holding_bars"], errors="coerce")
    _log(f"main {len(main_df)} / folds "
         f"{ {f: len(v) for f, v in folds.items()} } / fm_inc {len(fm_inc)}；"
         f"occ 有效 {int(main_df['occ'].notna().sum())}")

    _log(f"load lake OHLCV from {LOAD_FROM.date()}")
    base = _matrix(LAKE, ["close", "open", "high", "low", "volume", "amount"],
                   LOAD_FROM)
    close, open_, high, low = base["close"], base["open"], base["high"], base["low"]
    volume, amount = base["volume"], base["amount"]
    ret = close.pct_change(fill_method=None).astype("float32")

    _log("load daily_basic turnover/pb/mv")
    db = _matrix(DAILY_BASIC, ["turnover_rate", "turnover_rate_f", "pb", "circ_mv"],
                 LOAD_FROM)
    for k in db:
        db[k] = db[k].reindex(index=close.index, columns=close.columns)
    tr = db["turnover_rate"].astype("float32")
    trf = db["turnover_rate_f"].astype("float32")
    pb = db["pb"].astype("float32")
    cmv = db["circ_mv"].astype("float32")

    _log("mkt/industry EW")
    sb = pq.read_table("data_lake/stock_basic.parquet",
                       columns=["ts_code", "industry"]).to_pandas()
    ind_map = dict(zip(sb["ts_code"].astype(str), sb["industry"].astype(str)))
    ind_of = pd.Series([ind_map.get(s, np.nan) for s in close.columns],
                       index=close.columns, dtype=object)
    mkt = ret.mean(axis=1)
    ind_ret_syms = {}
    for name in sorted(set(ind_of.dropna().unique())):
        cols = ind_of.index[ind_of == name]
        ind_ret_syms[name] = ret[cols].mean(axis=1)
    # 行业收益宽表（逐行业块赋值）
    ind_wide = pd.DataFrame(np.nan, index=close.index, columns=close.columns,
                            dtype="float32")
    for name, ser in ind_ret_syms.items():
        cols = list(ind_of.index[ind_of == name])
        ind_wide[cols] = np.repeat(ser.astype("float32").to_numpy()[:, None],
                                   len(cols), axis=1)

    return dict(main_df=main_df, folds=folds, fm_inc=fm_inc, close=close,
                open=open_, high=high, low=low, volume=volume, amount=amount,
                ret=ret, tr=tr, trf=trf, pb=pb, cmv=cmv, mkt=mkt,
                ind_wide=ind_wide, ind_map=ind_map, ind_of=ind_of)


# ============================================================ 特征族生成器
def fam_bases(B):
    """共用中间量（惰性算一次）。"""
    if hasattr(fam_bases, "_cache"):
        return fam_bases._cache
    close, high, low, open_ = B["close"], B["high"], B["low"], B["open"]
    ret, volume = B["ret"], B["volume"]
    c = {}
    rng_ = (high - low) / (close.abs() + 1e-12)
    oc = (open_ - close.shift(1)).abs() / (close.shift(1).abs() + 1e-12)
    c["cc_vol"] = ret.rolling(20, min_periods=15).std()
    c["pk_vol"] = np.sqrt((rng_.astype("float32") ** 2).rolling(20, min_periods=15)
                          .mean() / (4 * np.log(2)))
    c["up_shadow"] = (high - np.maximum(open_, close)) / (close + 1e-12)
    c["dn_shadow"] = (np.minimum(open_, close) - low) / (close + 1e-12)
    c["cpos"] = (close - low) / (high - low + 1e-12)
    c["on_gap"] = open_ / close.shift(1) - 1.0
    c["in_ret"] = close / open_ - 1.0
    c["prev_close"] = close.shift(1)
    c["roll_max252"] = close.rolling(252, min_periods=200).max()
    c["roll_min252"] = close.rolling(252, min_periods=200).min()
    c["roll_max60"] = close.rolling(60, min_periods=50).max()
    c["is_nh60"] = (close >= c["roll_max60"] * 0.9999).astype("float32")
    c["absret"] = ret.abs().astype("float32")
    c["amt"] = B["amount"].astype("float32")
    c["streak"] = None   # 逐行迭代算
    s = np.sign(ret.to_numpy(dtype=np.float64))
    st = np.zeros_like(s)
    for i in range(1, s.shape[0]):
        same = (s[i] == s[i - 1]) & (s[i] != 0)
        st[i] = np.where(same, st[i - 1] + s[i], s[i])
    c["streak"] = pd.DataFrame(st.astype("float32"), index=close.index,
                               columns=close.columns)
    # 涨停带距离（主板1.1 / 创业板科创板1.2，2020-08-24 前创业板仍1.1——近似）
    prefix = pd.Series(close.columns).str[:3]
    ratio = np.where(prefix.isin(["300", "301", "688"]), 1.2, 1.1)
    cut = close.index >= pd.Timestamp("2020-08-24")
    ratio_m = np.tile(ratio.astype("float32"), (len(close.index), 1))
    ratio_m[~cut, :] = np.where(
        np.tile(prefix.isin(["300", "301"]).to_numpy()[:, None].T[0],
                (len(close.index[~cut]), 1)), 1.1, ratio_m[~cut, :])
    limit_price = c["prev_close"] * pd.DataFrame(ratio_m, index=close.index,
                                                 columns=close.columns)
    c["limit_dist"] = (limit_price - close) / (close + 1e-12)
    fam_bases._cache = c
    return c


def gen_family(name, B):
    """族 → {列名: wide DataFrame}。全部 ≤signal_date 无前视。"""
    F = fam_bases(B)
    close, volume, ret, tr = B["close"], B["volume"], B["ret"], B["tr"]
    out = {}
    W1 = [20, 60]
    for w in W1:
        out[f"tr_avg{w}"] = tr.rolling(w, min_periods=int(w * .8)).mean()
        out[f"amihud{w}"] = (F["absret"] / (F["amt"] + 1.0)).rolling(
            w, min_periods=int(w * .8)).mean()
        out[f"zeroret{w}"] = (ret.abs() < 0.0015).astype("float32").rolling(
            w, min_periods=int(w * .8)).mean()
        out[f"on_gap{w}"] = F["on_gap"].astype("float32").rolling(
            w, min_periods=int(w * .8)).mean()
        out[f"in_ret{w}"] = F["in_ret"].astype("float32").rolling(
            w, min_periods=int(w * .8)).mean()
        out[f"cpos{w}"] = F["cpos"].astype("float32").rolling(
            w, min_periods=int(w * .8)).mean()
        out[f"shadow_up{w}"] = F["up_shadow"].astype("float32").rolling(
            w, min_periods=int(w * .8)).mean()
        out[f"shadow_dn{w}"] = F["dn_shadow"].astype("float32").rolling(
            w, min_periods=int(w * .8)).mean()
        out[f"maxret{w}"] = ret.rolling(w, min_periods=int(w * .8)).max()
        out[f"skew{w}"] = ret.rolling(w, min_periods=int(w * .8)).skew()
        out[f"vol_stab{w}"] = volume.rolling(w, min_periods=int(w * .8)).std() / \
            (volume.rolling(w, min_periods=int(w * .8)).mean() + 1.0)
        out[f"pv_corr{w}"] = ret.rolling(w, min_periods=int(w * .8)).corr(volume)
        vv = volume.astype("float64")
        vwap = (close * vv).rolling(w, min_periods=int(w * .8)).sum() / \
            vv.rolling(w, min_periods=int(w * .8)).sum()
        out[f"vwap_pos{w}"] = (close / vwap - 1.0).astype("float32")
        out[f"limit_dist{w}"] = F["limit_dist"].astype("float32").rolling(
            w, min_periods=int(w * .8)).mean()
        out[f"bias{w}"] = (close / close.rolling(w, min_periods=int(w * .8))
                           .mean() - 1.0).astype("float32")
        out[f"turn_mom_dev{w}"] = (
            (ret * tr).rolling(w, min_periods=int(w * .8)).sum() /
            (tr.rolling(w, min_periods=int(w * .8)).sum() + 1e-6)
            - (close / close.shift(w) - 1.0)).astype("float32")
    out["tr_chg_5_60"] = (tr.rolling(5).mean() /
                          (tr.rolling(60).mean() + 1e-6)).astype("float32")
    # 当日换手 / 252 日均换手（rolling.rank 不存在，用均值比近似分位）
    out["tr_pct252"] = (tr / (tr.rolling(252, min_periods=200).mean()
                              + 1e-6)).astype("float32")
    out["pk_ratio20"] = (F["pk_vol"] / (F["cc_vol"] + 1e-12)).astype("float32")
    out["kurt60"] = ret.rolling(60, min_periods=48).kurt().astype("float32")
    out["high252"] = (close / F["roll_max252"] - 1.0).astype("float32")
    out["low252"] = (close / F["roll_min252"] - 1.0).astype("float32")
    out["nh60_20"] = F["is_nh60"].rolling(20, min_periods=15).mean().astype("float32")
    out["streak"] = F["streak"].astype("float32")
    out["gap_cnt20"] = (F["on_gap"].abs() > 0.01).astype("float32").rolling(
        20, min_periods=15).mean()
    out["vol_ratio_20_60"] = (ret.rolling(20).std() /
                              (ret.rolling(60).std() + 1e-12)).astype("float32")
    out["vov20"] = F["absret"].rolling(20, min_periods=15).std().astype("float32")
    xm = ret.mul(B["mkt"], axis=0).rolling(250, min_periods=200).mean()
    rm_ = ret.rolling(250, min_periods=200).mean()
    mm_ = B["mkt"].rolling(250, min_periods=200).mean()
    mv_ = B["mkt"].rolling(250, min_periods=200).var()
    out["beta250"] = (xm.sub(rm_.mul(mm_, axis=0)).div(mv_, axis=0)).astype("float32")
    resid_ind = ret - B["ind_wide"]
    out["idio_vol60"] = resid_ind.rolling(60, min_periods=48).std().astype("float32")
    out["ind_corr60"] = ret.rolling(60, min_periods=48).corr(B["ind_wide"])
    ma20 = close.rolling(20, min_periods=15).mean()
    sd20 = close.rolling(20, min_periods=15).std()
    out["boll_pos20"] = ((close - ma20) / (2 * sd20 + 1e-12)).astype("float32")
    # 日内截面秩（基本面族：当日全湖截面排名）
    cmv_r = B["cmv"].rank(axis=1, pct=True)
    out["wic_size"] = np.log(cmv_r + 1e-6).astype("float32")
    out["wic_pb"] = B["pb"].rank(axis=1, pct=True).astype("float32")
    out["pb_chg60"] = (B["pb"] / B["pb"].shift(60) - 1.0).astype("float32")
    out["wic_trf"] = B["trf"].rank(axis=1, pct=True).astype("float32")
    out["wic_amount"] = B["amount"].rank(axis=1, pct=True).astype("float32")
    return {k: v.astype("float32") for k, v in out.items() if v is not None}


def gen_wave2(name, B):
    """守卫族：moneyflow 主力净流入强度（margin_secs 仅 2 天数据、cyq_chips
    为原始分布且 2024-04 起覆盖不足 60% 门槛——两族弃用，日志留痕）。"""
    out = {}
    try:
        mfb = _matrix("data_lake/moneyflow.parquet", ["net_mf_amount"],
                      LOAD_FROM)["net_mf_amount"]
        mfb = mfb.reindex(index=B["close"].index, columns=B["close"].columns)
        base = (mfb / (B["amount"] + 1.0)).astype("float32")
        for w in (20, 60):
            out[f"mf_net{w}"] = base.rolling(w, min_periods=int(w * .8)).mean()
    except Exception as e:
        _log(f"[wave2] moneyflow 跳过: {e}")
    _log("[wave2] margin_secs(仅2天)/cyq_chips(覆盖<60%) 弃用")
    if not out:
        raise RuntimeError("wave2 全部守卫族不可用")
    return out


# ============================================================ 主流程
def main():
    global DEADLINE
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=10.0)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    DEADLINE = T0 + args.hours * 3600
    os.makedirs(OUT_DIR, exist_ok=True)
    bonf = ALPHA / N_TOTAL
    _progress(f"\n## 因子动物园启动 {pd.Timestamp.now()}（--hours {args.hours}"
              f"{' smoke' if args.smoke else ''}）")
    _progress(f"- 预登记：G1 p<{bonf:.2e}(N_TOTAL={N_TOTAL}) ∧|ΔQ5−Q1|≥{MIN_DQ}"
              f"；G2 ≥4/5 年；G3 wf 双折；G5 置换 p<0.05 ∧ 截面IC 同号(B={B_PERM})")
    _progress(f"- 目标轴：Y_pnl=avg_pnl_pct / Y_occ=wait_days+holding_bars")
    _log(f"prereg bonf={bonf:.2e}")

    B = build_bases()
    main_df, folds, fm_inc = B["main_df"], B["folds"], B["fm_inc"]

    # 断点续跑：已测键跳过
    reg_path = os.path.join(OUT_DIR, "registry.jsonl")
    tested = set()
    if os.path.exists(reg_path):
        for ln in open(reg_path, encoding="utf-8"):
            try:
                r = json.loads(ln)
                tested.add((r["feature"], r["target"]))
            except Exception:
                pass
    survivors = []
    n_round = 0

    def eval_and_register(col, values):
        nonlocal survivors
        fm_inc[col] = values["fm_inc"]     # 复现对比需要（round0 列已存在则同值覆盖）
        for target in ("avg_pnl_pct", "occ"):
            if (col, target) in tested:
                continue
            res = evaluate(col, target, values, main_df, folds, RNG, bonf)
            with open(reg_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
            tested.add((col, target))
            if res.get("survivor"):
                # 登记册字段：fullmkt 增量方向复现（非闸）
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
                _log(f"  ★ SURVIVOR {col}×{target}: IC {res['ic']} "
                     f"yrs {res['years_same']}/5 p{res['struct_emp_p']} "
                     f"wic {res['within_date_ic']} fmΔ {res.get('fm_inc_contrast')}")
            elif res.get("gate_G1") and res.get("gate_G2") and res.get("gate_G3"):
                _log(f"  ~ {col}×{target}: 三闸过 G5 拒 p={res['struct_emp_p']} "
                     f"wic={res['within_date_ic']}")
        return

    def wide_to_values(col, wide):
        return {"main": _lk(wide, main_df["signal_date"], main_df["symbol"]),
                **{f: _lk(wide, folds[f]["signal_date"], folds[f]["symbol"])
                   for f in FOLD_KEYS},
                "fm_inc": _lk(wide, fm_inc["signal_date"], fm_inc["symbol"])}

    # —— 轮 0：既有 at_signal 特征 × occ 轴（R7d 结构补检）——
    n_round += 1
    _log(f"── 轮 {n_round}: 既有特征 × occ（R7d 补检）")
    _progress(f"\n### 轮 {n_round} 既有特征×occ（R7d 补检） {pd.Timestamp.now()}")
    for c in EXISTING_AT_SIGNAL:
        if c not in main_df.columns:
            continue
        vals = {"main": main_df[c].to_numpy(dtype=float),
                **{f: folds[f][c].to_numpy(dtype=float) for f in FOLD_KEYS},
                "fm_inc": fm_inc[c].to_numpy(dtype=float) if c in fm_inc.columns
                else np.full(len(fm_inc), np.nan)}
        eval_and_register(c, vals)
    _progress(f"- 完成：{len(EXISTING_AT_SIGNAL)} 列")

    # —— 波 1：价格量能族（全网格一次生成，分批评估控内存）——
    fams = gen_family("wave1", B)
    keys = sorted(fams)
    if args.smoke:
        keys = keys[:6]
    batch, batch_cols = 12, []
    n_round += 1
    _log(f"── 轮 {n_round}: 波1 {len(keys)} 列（{args.smoke and 'smoke' or 'full'}）")
    _progress(f"\n### 轮 {n_round} 波1 价格量能族 {len(keys)} 列 {pd.Timestamp.now()}")
    t_fam = time.time()
    for i, k in enumerate(keys):
        try:
            vals = wide_to_values(k, fams[k])
            eval_and_register(k, vals)
        except Exception as e:
            _log(f"  ! {k} 失败: {e}")
        if (i + 1) % 12 == 0:
            _progress(f"- {i + 1}/{len(keys)}（{time.time() - t_fam:.0f}s）")
            if time.time() > DEADLINE:
                _progress("- ⏰ 预算到，跳剩余波1")
                break
    del fams
    _progress(f"- 波1 完成，累计存活 {len(survivors)}")

    # —— 波 2：守卫族（moneyflow/margin/cyq）——
    if not args.smoke and time.time() < DEADLINE:
        n_round += 1
        _log(f"── 轮 {n_round}: 波2 守卫族")
        _progress(f"\n### 轮 {n_round} 波2 守卫族 {pd.Timestamp.now()}")
        try:
            w2 = gen_wave2("wave2", B)
            for k, wide in w2.items():
                try:
                    vals = wide_to_values(k, wide)
                    if np.isfinite(vals["main"]).mean() < 0.6:
                        _log(f"  ~ {k} 覆盖 {np.isfinite(vals['main']).mean():.0%}"
                             f" <60% 跳过")
                        continue
                    eval_and_register(k, vals)
                except Exception as e:
                    _log(f"  ! {k} 失败: {e}")
        except Exception as e:
            _progress(f"- 波2 整体跳过: {e}")
        _progress(f"- 波2 完成，累计存活 {len(survivors)}")

    # —— 交互：各轴 top5 by |wic| 的组内秩乘积（≤10 对/轴）——
    if time.time() < DEADLINE and not args.smoke:
        n_round += 1
        _log(f"── 轮 {n_round}: 交互")
        _progress(f"\n### 轮 {n_round} 交互（轴内 top5×top5） {pd.Timestamp.now()}")
        from itertools import combinations
        from scipy import stats as _st
        for target in ("avg_pnl_pct", "occ"):
            pool = [r for r in survivors if r["target"] == target
                    and r.get("within_date_ic") is not None]
            # 交互原料放宽到三闸过者（从 registry 重读）
            if len(pool) < 2:
                regs = [json.loads(l) for l in open(reg_path, encoding="utf-8")]
                pool = [r for r in regs if r["target"] == target
                        and r.get("gate_G1") and r.get("gate_G2")
                        and r.get("gate_G3") and r.get("within_date_ic") is not None]
            pool = sorted(pool, key=lambda r: -abs(r["within_date_ic"]))[:5]
            cols = [r["feature"] for r in pool]
            made = []
            for a, b in combinations(cols, 2):
                cname = f"{a}#{b}"
                if (cname, target) in tested:
                    continue
                def _z(v, dates):
                    df_ = pd.DataFrame({"d": dates, "v": v})
                    return df_.groupby("d")["v"].rank(pct=True).to_numpy() - 0.5
                if a not in main_df.columns or b not in main_df.columns:
                    continue
                prod = _z(main_df[a].to_numpy(dtype=float),
                          main_df["signal_date"]) * \
                    _z(main_df[b].to_numpy(dtype=float),
                       main_df["signal_date"])
                vals = {"main": prod,
                        "fm_inc": np.full(len(fm_inc), np.nan)}
                for f in FOLD_KEYS:
                    vals[f] = _z(folds[f][a].to_numpy(dtype=float),
                                 folds[f]["signal_date"]) * \
                        _z(folds[f][b].to_numpy(dtype=float),
                           folds[f]["signal_date"])
                res = evaluate(cname, target, vals, main_df, folds, RNG, bonf)
                with open(reg_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
                tested.add((cname, target))
                if res.get("survivor"):
                    survivors.append(res)
                    _log(f"  ★ SURVIVOR {cname}×{target}")
                made.append(cname)
            _progress(f"- {target} 轴交互 {len(made)} 对")

    # —— 终报 ——
    _log("写终报")
    _progress(f"\n## 终报 {pd.Timestamp.now()}（总 {(time.time() - T0) / 60:.0f}min）")
    # 冗余去重呈现：|ρ|>0.9 保留 |wic| 强者
    final = []
    for r in sorted(survivors,
                    key=lambda r: -abs(r.get("within_date_ic") or 0)):
        dup = False
        for kept in final:
            if kept["target"] != r["target"]:
                continue
            if r["feature"] in main_df.columns and kept["feature"] in main_df.columns:
                rho = main_df[r["feature"]].corr(main_df[kept["feature"]],
                                                 method="spearman")
                if rho == rho and abs(rho) > 0.9:
                    r["redundant_with"] = kept["feature"]
                    dup = True
                    break
        if not dup:
            final.append(r)
    out = {"preregistration": {
               "gates": ["G1 p<%.2e & |dq51|>=%s" % (bonf, MIN_DQ),
                         "G2 >=4/5 年", "G3 wf 双折",
                         "G5 置换 p<0.05 ∧ 截面IC 同号"],
               "n_total": N_TOTAL, "b_perm": B_PERM,
               "targets": {"avg_pnl_pct": "质量轴", "occ": "吞吐轴(占用天数)"},
               "note": "登记册条目≠采纳；采纳裁决归用户；C2/ADR-16 未动"},
           "n_tested": len(tested),
           "survivors_raw": survivors,
           "survivors_final": final,
           "generated_at": pd.Timestamp.now().isoformat()}
    with open(os.path.join(OUT_DIR, "survivors.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    L = ["# 因子动物园终报（双轴 · 2026-08-29 夜）", "",
         f"> 闸：G1 p<{bonf:.2e} ∧|ΔQ5−Q1|≥{MIN_DQ}；G2 ≥4/5 年；G3 wf 双折；"
         f"G5 同日置换 p<0.05 ∧ 截面IC 同号。测试总数 {len(tested)}。",
         "> 存活=登记册条目，非采纳——采纳裁决归用户晨会。", ""]
    for target, label in (("avg_pnl_pct", "质量轴（预测单笔盈亏）"),
                          ("occ", "吞吐轴（预测资金占用）")):
        rows = [r for r in final if r["target"] == target]
        L.append(f"## {label}：{len(rows)} 个")
        L.append("")
        for r in rows:
            L.append(f"- **{r['feature']}**（方向 {r['direction']:+d}，"
                     f"IC {r['ic']:+.4f}，截面IC {r.get('within_date_ic')}，"
                     f"置换p {r.get('struct_emp_p')}，年段 {r['years_same']}/5，"
                     f"折 {r.get('fold_contrasts')}，"
                     f"fm增量Δ {r.get('fm_inc_contrast')}）")
        L.append("")
    with open(os.path.join(OUT_DIR, "final_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    _log(f"[done] {OUT_DIR}/final_report.md；存活 {len(final)}"
         f"（raw {len(survivors)}）；总 {(time.time() - T0) / 60:.0f}min")


if __name__ == "__main__":
    main()
