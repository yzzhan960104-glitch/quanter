# -*- coding: utf-8 -*-
"""H-R7a 确认样本 · 全市场 × 2021 起（2026-08-26 用户裁决「数据用全市场，2021 起」）。

════════════════════════════════════════════════════════════════════
预登记协议（先于看数写死——本脚本作者此刻未见过任何全市场回测结果）
════════════════════════════════════════════════════════════════════
背景：P2 主映射（线性 [0.03,0.10]）五闸否决；事后敏感性发现均值匹配映射在
创板科创池五年全正（外层 Δ+8.8pp）——事后观察有偷看嫌疑，需新样本确认。
用户裁决：用全市场（沪深 A 股）2021-2026 作横截面新样本（假设生成过程从未
消费过主板个股），立即确认，不等 20 交易日前向窗。

样本定义：
  universe = 沪深全市场（代码前缀 60/68/00/30；排除北交所 30cm 域与 ETF——
  ETF 不在股票湖）× 近 30 日均成交额 ≥1 亿元（与基线同闸）× 数据 2021-01-01 起
  （与对照线口径同起点）；
受测假设（冻结，不重选）：
  特征 = P1 存活且 at_signal 的全部特征（bottom_disp/vol5_slope，方向各 +1，
  从 logs/quality/survivors.json 读入断言——禁止在本样本上重跑 P1 挑特征）；
  映射 = 均值匹配 pos_cap = clip(0.075 + 0.07×(p−0.5), 0.03, 0.10)，p=分数对
  训练段 CDF 的分位；打分 = 方向对齐 z 等权（主型）/训练段 IC 加权（副型）；
  训练段 = 本样本 2022-2024（时序切分，禁随机 CV）；
六闸（全过 = 确认存活；任一否 = 翻号入台账）：
  ① 外层 2026 冻结口径 ann：质量臂 > 固定臂；
  ② 逐年段 2022-2026（冻结分段）：Δ ≥ −0.02/年（>2pp 恶化即否）；
  ③ 全期 2021-2026 冻结口径 ann：质量臂 > 固定臂（防外层单年侥幸）；
  ④ 滑点 25bps 下外层：质量臂 > 固定臂；
  ⑤ 敞口公平性：质量臂 taken 平均 pos_cap ∈ [0.070, 0.080]（防再犯 P2 机械差）；
  ⑥ 拥挤日（same_day_n>80，全池口径）：taken 均值 质量 ≥ 固定 − 0.5pp；
边界声明：本样本过闸 ≠ 主板可上线（B3 参数为创板科创 20cm 调校）；它确认的是
「质量倾斜假设」的横截面泛化。主板-only 严格 OOS 切片（不含原池个股）另作
诊断报告（非闸）——全市场样本含原 64,532 笔创板科创成交，闸结果可能被原效应
携带，严格切片用于辨别。
════════════════════════════════════════════════════════════════════

用法：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_r7a_fullmarket.py
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from diag.quality_p0_features import STATE_PATH, _scan_pool
from diag.quality_p2_layering import (_ann, _pm, _to_filled, _taken,
                                      apply_scores)

OUT_DIR = "logs/quality"
BOARD_PREFIX_OK = ("60", "68", "00", "30")   # 沪主板/科创/深主板/创业（排北交所）
MAINMKT_PREFIX = ("30", "31", "68", "69")     # 原基线池（创板科创）——严格切片用


def load_universe_full(start="2021-01-01", min_amt=1e5):
    """沪深全市场 universe（镜像 discovery.snapshot.load_universe，板别放宽）。"""
    lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                           filters=[("date", ">=", pd.Timestamp(start))])
    syms = lake.index.get_level_values("symbol").unique().tolist()
    amt = lake.groupby("symbol")["amount"].apply(
        lambda s: s.tail(30).mean() if len(s) > 0 else 0.0)
    def ok(s):
        c = s.split(".")[0]
        return c[:2] in BOARD_PREFIX_OK
    tradable = [s for s in syms if ok(s) and amt.get(s, 0.0) >= min_amt]
    universe = {}
    for s in tradable:
        try:
            universe[s] = lake.xs(s, level="symbol").sort_index()
        except Exception:
            continue
    return universe


def main():
    from discovery.split import Segment, holdout_split

    os.makedirs(OUT_DIR, exist_ok=True)
    # —— 受测特征冻结断言（从 P1 产物读入，禁止本样本重选）——
    surv = json.load(open(os.path.join(OUT_DIR, "survivors.json"),
                          encoding="utf-8"))
    feats = {s["feature"]: s["direction"] for s in surv["survivors"]
             if s["feature"] in ("bottom_disp", "vol5_slope")}
    assert set(feats) == {"bottom_disp", "vol5_slope"}, \
        f"P1 存活集合异常：{feats}（H-R7a 冻结域被破坏）"
    print(f"[prereg] 特征冻结：{feats}；映射=均值匹配 0.075±0.035 clip；"
          f"训练 2022-24；六闸见脚本头", flush=True)

    base = json.load(open(STATE_PATH, encoding="utf-8"))["base"]
    print("[fullmkt] 加载全市场 universe（2021 起）...", flush=True)
    universe = load_universe_full("2021-01-01")
    print(f"[fullmkt] universe={len(universe)} 只", flush=True)

    df, n_fail = _scan_pool("fullmkt", base, universe)
    out_parquet = os.path.join(OUT_DIR, "trades_features_fullmkt.parquet")
    df.to_parquet(out_parquet, index=False)
    print(f"[fullmkt] 特征库落盘 {out_parquet} rows={len(df)} fail={n_fail}",
          flush=True)

    # —— 评估设施（同 P2 口径）——
    import pyarrow.parquet as pq
    _dates = pq.read_table("data_lake/a_shares_daily.parquet",
                           columns=["date"]).column("date").to_pandas()
    udates = pd.DatetimeIndex(sorted(_dates.unique()))
    udates = udates[udates >= pd.Timestamp("2021-01-01")]

    split = holdout_split()
    outer = split.outer
    seg_full = Segment("full", date(2021, 1, 1), date(2026, 12, 31))
    train = df[(df["year"] >= 2022) & (df["year"] <= 2024)]

    def _mm_caps(d):
        d = d.copy()
        d["pos_cap"] = (0.075 + 0.07 * (d["score_pct"] - 0.5)).clip(0.03, 0.10)
        return d

    results = {"feats": feats, "sample": "fullmarket_2021", "modes": {}}
    dfq_by_mode = {}
    for mode in ("equal", "ic"):
        dfq, zparams, ics = apply_scores(df, feats, mode=mode, train_df=train)
        dfq = _mm_caps(dfq)
        dfq_by_mode[mode] = dfq
        fixed = _to_filled(df)
        quality = _to_filled(dfq)
        pm_f, pm_q = _pm(), _pm(quality_alloc=True)

        f_out = _ann([t for t in fixed if outer.covers(t["signal_date"])],
                     outer, udates, pm_f)
        q_out = _ann([t for t in quality if outer.covers(t["signal_date"])],
                     outer, udates, pm_q)
        g1 = q_out["ann"] > f_out["ann"]
        f_full = _ann(fixed, seg_full, udates, pm_f)
        q_full = _ann(quality, seg_full, udates, pm_q)
        g3 = q_full["ann"] > f_full["ann"]
        f25 = _ann([t for t in fixed if outer.covers(t["signal_date"])],
                   outer, udates, _pm(slippage_bps=25))
        q25 = _ann([t for t in quality if outer.covers(t["signal_date"])],
                   outer, udates, _pm(slippage_bps=25, quality_alloc=True))
        g4 = q25["ann"] > f25["ann"]

        yearly = {}
        g2 = True
        for y in (2022, 2023, 2024, 2025, 2026):
            seg = Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
            fy = _ann([t for t in fixed if seg.covers(t["signal_date"])],
                      seg, udates, pm_f)
            qy = _ann([t for t in quality if seg.covers(t["signal_date"])],
                      seg, udates, pm_q)
            yearly[y] = {"fixed": fy, "quality": qy,
                         "delta": round(qy["ann"] - fy["ann"], 4)}
            if yearly[y]["delta"] < -0.02:
                g2 = False

        taken_q = _taken([t for t in quality if outer.covers(t["signal_date"])],
                         pm_q)
        mean_cap = round(float(np.mean([t["pos_cap"] for t in taken_q])), 4) \
            if taken_q else None
        g5 = mean_cap is not None and 0.070 <= mean_cap <= 0.080

        taken_f = _taken([t for t in fixed if outer.covers(t["signal_date"])],
                         pm_f)
        mf = [t["avg_pnl_pct"] for t in taken_f if t["same_day_n"] > 80]
        mq = [t["avg_pnl_pct"] for t in taken_q if t["same_day_n"] > 80]
        m_f = round(float(np.mean(mf)), 2) if mf else None
        m_q = round(float(np.mean(mq)), 2) if mq else None
        g6 = m_f is not None and m_q is not None and m_q >= m_f - 0.5

        verdict = "PASS" if (g1 and g2 and g3 and g4 and g5 and g6) else "VETO"
        results["modes"][mode] = {
            "g1_outer": {"fixed": f_out, "quality": q_out,
                         "delta": round(q_out["ann"] - f_out["ann"], 4),
                         "pass": g1},
            "g2_yearly": yearly | {"pass": g2},
            "g3_full": {"fixed": f_full, "quality": q_full,
                        "delta": round(q_full["ann"] - f_full["ann"], 4),
                        "pass": g3},
            "g4_slip25": {"fixed": f25, "quality": q25,
                          "delta": round(q25["ann"] - f25["ann"], 4),
                          "pass": g4},
            "g5_exposure": {"mean_pos_cap": mean_cap, "pass": g5},
            "g6_crowded": {"fixed_mean": m_f, "quality_mean": m_q,
                           "n_f": len(mf), "n_q": len(mq), "pass": g6},
            "verdict": verdict,
        }
        print(f"[{mode}] ①{int(g1)} ②{int(g2)} ③{int(g3)} ④{int(g4)} "
              f"⑤{int(g5)} ⑥{int(g6)} → {verdict} "
              f"(外层Δ{q_out['ann'] - f_out['ann']:+.3f} 全期Δ"
              f"{q_full['ann'] - f_full['ann']:+.3f})", flush=True)

    # —— 主板-only 严格 OOS 切片（诊断非闸：全市场闸可能被原池效应携带）——
    def _is_mainboard(sym):
        return sym.split(".")[0][:2] not in MAINMKT_PREFIX
    diag = {}
    for mode in ("equal", "ic"):
        dfq = dfq_by_mode[mode]
        mb = df[df["symbol"].map(_is_mainboard)]
        mbq = dfq[dfq["symbol"].map(_is_mainboard)]
        fmb = _ann(_to_filled(mb), seg_full, udates, _pm())
        qmb = _ann(_to_filled(mbq), seg_full, udates, _pm(quality_alloc=True))
        fmb_o = _ann([t for t in _to_filled(mb) if outer.covers(t["signal_date"])],
                     outer, udates, _pm())
        qmb_o = _ann([t for t in _to_filled(mbq) if outer.covers(t["signal_date"])],
                     outer, udates, _pm(quality_alloc=True))
        diag[mode] = {"n_mb": int(len(mb)),
                      "full_delta": round(qmb["ann"] - fmb["ann"], 4),
                      "outer_delta": round(qmb_o["ann"] - fmb_o["ann"], 4),
                      "mb_fixed_outer": fmb_o, "mb_quality_outer": qmb_o}
        print(f"[mb-only/{mode}] 主板 {len(mb)} 笔：全期Δ"
              f"{qmb['ann'] - fmb['ann']:+.3f} 外层Δ"
              f"{qmb_o['ann'] - fmb_o['ann']:+.3f} "
              f"(主板基线外层 {fmb_o['ann']:+.1%})", flush=True)
    results["mainboard_only_diag"] = diag
    results["verdict_primary"] = results["modes"]["equal"]["verdict"]

    with open(os.path.join(OUT_DIR, "r7a_fullmarket.json"), "w",
              encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1, default=str)
    print(f"[done] {OUT_DIR}/r7a_fullmarket.json "
          f"verdict={results['verdict_primary']}", flush=True)


if __name__ == "__main__":
    main()
