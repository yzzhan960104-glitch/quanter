# -*- coding: utf-8 -*-
"""因子家族淘汰赛 · Phase 0：家族归并与代表挑选（2026-08-29 · 方案定稿）。

预登记（先于运行写死，全文见 docs/superpowers/plans/
2026-08-29-factor-family-tournament.md）：
  1. 轴内 Spearman |ρ|>0.7 连通分量=家族；交互列(#)不参赛只列示；
  2. family_score = median(|wic|) × median(years_same/5) × fm复现率
     × min(成员数,3)^0.5；fm 缺失按 0.5 中性计（波3 写序瑕疵）；
  3. 机理白名单（七类=方案六类+估值族，估值族偏离已在方案注记）：
     波动 / 流动性 / 估值 / 几何 / 趋势效率 / 资金流 / 热股冻槽；
     白名单外 → 观察族（不参赛）；
  4. 代表规则（按序）：白名单→5/5 年→|wic|→成本低→流动性族带滑点义务；
  5. 每家族 1 主 + 1 副代表（副代表仅替补）。

用法：PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_zoo_families.py
产物：logs/quality/factor_zoo/families.{md,json}
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from diag.quality_zoo_loop import (FOLD_KEYS, build_bases, fam_bases,
                                    gen_family, gen_wave2)
from diag.quality_momentum_batch2 import _lk
from diag.quality_zoo_wave3 import gen_wave3

REG = "logs/quality/factor_zoo/registry.jsonl"
OUT_DIR = "logs/quality/factor_zoo"
RHO_TH = 0.7
T0 = time.time()


def _log(m):
    print(f"[{time.time() - T0:>5.0f}s] {m}", flush=True)


MECH = [
    ("波动", ["pk_vol", "gk_vol", "rs_vol", "tail_spread", "vol20_pct252",
              "idio_vol", "semi_ratio", "vov", "vol_ratio", "vol_stab",
              "shadow_dn", "shadow_up", "dd", "ulcer", "extreme", "atr_pct"]),
    ("流动性", ["amihud", "wic_amount", "wic_trf", "tr_pct252", "tr_chg",
               "tr_std", "tr_skew", "vol_top1", "quiet_share", "shrink_share",
               "hi_vol", "zeroret", "upvol", "obv", "ad_slope", "cmf",
               "mfi14", "absret_vol", "pv_corr", "float_ratio", "turn_mom",
               "vol5_slope", "bvr"]),
    ("估值", ["wic_pb", "wic_pe", "wic_ps", "pe_chg", "pb_chg", "log_price",
             "round_num", "wic_size"]),
    ("几何", ["suppression", "neckline_span", "touches", "neck_rel", "h_rel",
             "risk_pct_v", "bottom_disp", "pattern_days", "n_tops", "h_atr",
             "rr_id", "same_day"]),
    ("趋势效率", ["kaufman", "ma_stack", "ma_spread", "ma20_slope", "dmi",
                 "bias", "decay_mom", "rank_mom", "ac1", "ac5", "alternation",
                 "pos_share", "on_gap", "in_ret", "cpos", "ind_corr",
                 "mkt_corr", "ex_mkt", "ind_rank", "kdj_kd", "kdj_j",
                 "wr14", "streak", "wick_price"]),
    ("资金流", ["mf_net", "sm_net", "elg_net", "elg_pos"]),
    ("热股冻槽", ["alpha", "ret20", "ret60", "maxret", "high252", "low252",
                 "nh60_20", "gap_cnt", "limit_up", "limit_touch", "one_board",
                 "kaufman_er"]),
]


def mech_of(feat):
    for name, prefs in MECH:
        if any(feat.startswith(p) for p in prefs):
            return name
    return None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    recs = [json.loads(l) for l in open(REG, encoding="utf-8")]
    surv = [r for r in recs if r.get("survivor") and "#" not in r["feature"]]
    inter = [r for r in recs if r.get("survivor") and "#" in r["feature"]]
    _log(f"存活 {len(surv)} 单特征 + {len(inter)} 交互（交互不参赛）")

    # —— fm 回填：registry 写序 bug 致 fm 只在 stdout 日志——从 ★ 行解析 ——
    import re
    fm_map = {}
    for path in ("logs/quality_zoo_run.log", "logs/quality_zoo_wave3_run.log"):
        if not os.path.exists(path):
            continue
        for ln in open(path, encoding="utf-8", errors="ignore"):
            m = re.search(r"★\s+(?:SURVIVOR\s+)?(\S+?)×(avg_pnl_pct|occ):"
                          r".*?fmΔ ([-\d.]+)", ln)
            if m:
                fm_map[(m.group(1), m.group(2))] = float(m.group(3))
    for r in surv:
        if r.get("fm_inc_contrast") is None:
            r["fm_inc_contrast"] = fm_map.get((r["feature"], r["target"]))
    _log(f"fm 回填 {len(fm_map)} 条")

    # —— 重算全部存活列（三波生成器 + parquet 现成 + wave3 笔级）——
    B = build_bases()
    F = fam_bases(B)
    main_df = B["main_df"]
    wides = {}
    for gen in (lambda: gen_family("w1", B), lambda: gen_wave2("w2", B),
                lambda: gen_wave3(B, F)):
        try:
            wides.update(gen())
        except Exception as e:
            _log(f"生成器部分失败(容忍): {e}")
    td, ts = main_df["signal_date"], main_df["symbol"]
    for k, w in wides.items():
        main_df[k] = _lk(w, td, ts)
    del wides
    # wave3 笔级 + parquet 现成列
    sd = pd.to_datetime(main_df["signal_date"])
    main_df["neck_rel"] = main_df["neckline"] / main_df["entry"]
    main_df["h_rel"] = main_df["H"] / main_df["entry"]
    main_df["risk_pct_v"] = pd.to_numeric(main_df["risk_pct"], errors="coerce")
    main_df["is_monday"] = (sd.dt.dayofweek == 0).astype(float)
    main_df["is_friday"] = (sd.dt.dayofweek == 4).astype(float)
    dim = sd.dt.days_in_month
    main_df["turn_of_month"] = ((sd.dt.day <= 3) | (sd.dt.day >= dim - 3)) \
        .astype(float)
    _log(f"重算列完成，main_df {main_df.shape[1]} 列")

    # —— 轴内家族聚类 ——
    report = {}
    for target, label in (("avg_pnl_pct", "质量轴"), ("occ", "吞吐轴")):
        members = [r for r in surv if r["target"] == target
                   and r["feature"] in main_df.columns]
        feats = sorted({r["feature"] for r in members})
        _log(f"{label}: {len(feats)} 参赛列")
        if len(feats) < 2:
            continue
        sub = main_df[feats].astype(float)
        corr = sub.corr(method="spearman").abs()
        # 连通分量（并查集）
        parent = {f: f for f in feats}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, a in enumerate(feats):
            for b in feats[i + 1:]:
                if corr.loc[a, b] > RHO_TH:
                    parent[find(a)] = find(b)
        comps = {}
        for f in feats:
            comps.setdefault(find(f), []).append(f)

        fams = []
        for root, cols in comps.items():
            rs = {c: next(r for r in members if r["feature"] == c) for c in cols}
            wics = [abs(r.get("within_date_ic") or 0) for r in rs.values()]
            yrs = [(r.get("years_same") or 0) / 5 for r in rs.values()]
            fms = [np.sign(r.get("fm_inc_contrast")) == np.sign(r["direction"])
                   for r in rs.values() if r.get("fm_inc_contrast") is not None]
            fm_rate = float(np.mean(fms)) if fms else 0.5
            # 主导机理判读（修正注记：连通分量的传递链会把多机理熔成一簇，
            # 方案本意是"机理可讲通"——取成员多数派机理为族标签）
            from collections import Counter
            mc = Counter(m for m in (mech_of(c) for c in cols) if m)
            dominant, cnt = (mc.most_common(1)[0] if mc else (None, 0))
            fams.append({
                "family_id": f"{label[:1]}{len(fams) + 1}",
                "members": cols,
                "n": len(cols),
                "mech": [dominant] if dominant else [],
                "mech_mix": dict(mc),
                "in_whitelist": dominant is not None and cnt / len(cols) >= 0.5,
                "median_wic": round(float(np.median(wics)), 4),
                "median_years": round(float(np.median(yrs)), 3),
                "fm_rate": round(fm_rate, 3),
                "score": round(float(np.median(wics) * np.median(yrs) * fm_rate
                                    * min(len(cols), 3) ** 0.5), 4),
                "stats": {c: {"dir": rs[c]["direction"],
                              "ic": rs[c]["ic"],
                              "wic": rs[c].get("within_date_ic"),
                              "yrs": rs[c].get("years_same"),
                              "fm": rs[c].get("fm_inc_contrast")}
                          for c in cols},
            })
        fams.sort(key=lambda f: -f["score"])

        # —— 代表挑选（白名单→5/5→|wic|→成本低）——
        COST = {"parquet": 0, "cheap": 1, "heavy": 2}

        def cost_tier(c):
            if c in ("suppression", "neckline_span", "touches", "bottom_disp",
                     "pattern_days", "n_tops", "h_atr", "rr_id", "same_day_n",
                     "neck_rel", "h_rel", "risk_pct_v", "is_monday",
                     "is_friday", "turn_of_month"):
                return 0
            if any(c.startswith(p) for p in ("alpha",)):
                return 2
            return 1

        for f_ in fams:
            if not f_["in_whitelist"]:
                f_["rep"], f_["rep2"] = None, None
                f_["verdict"] = "观察族(白名单外)"
                continue
            cand = sorted(f_["members"],
                          key=lambda c: (-(f_["stats"][c]["yrs"] == 5),
                                         -abs(f_["stats"][c]["wic"] or 0),
                                         cost_tier(c)))
            f_["rep"], f_["rep2"] = cand[0], cand[1] if len(cand) > 1 else None
            f_["verdict"] = "参赛"
        report[target] = fams
        _log(f"{label}: {len(fams)} 家族；参赛 "
             f"{sum(1 for f_ in fams if f_['verdict'] == '参赛')}")

    with open(os.path.join(OUT_DIR, "families.json"), "w", encoding="utf-8") as f:
        json.dump({"rho_th": RHO_TH, "whitelist": [m[0] for m in MECH],
                   "families": report,
                   "interactions_excluded": [r["feature"] for r in inter],
                   "generated_at": pd.Timestamp.now().isoformat()},
                  f, ensure_ascii=False, indent=1, default=str)

    # —— md ——
    L = ["# Phase 0 · 家族归并与代表挑选（2026-08-29）", "",
         f"> ρ>{RHO_TH} 连通分量；评分=median|wic|×median年段×fm复现率×√min(n,3)；"
         "白名单七类；交互列不参赛。", ""]
    for target, label in (("avg_pnl_pct", "质量轴（仓位分层用途）"),
                          ("occ", "吞吐轴（同日优先级用途）")):
        L += [f"## {label}", "",
              "| 族 | 机理 | 成员数 | median|wic| | median年段 | fm复现 | 评分 | 主代表 | 副代表 | 裁决 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for f_ in report.get(target, []):
            L.append(
                f"| {f_['family_id']} | {'/'.join(f_['mech'])} | {f_['n']} | "
                f"{f_['median_wic']} | {f_['median_years']} | {f_['fm_rate']} | "
                f"{f_['score']} | {f_.get('rep') or '—'} | "
                f"{f_.get('rep2') or '—'} | {f_['verdict']} |")
        L.append("")
        for f_ in report.get(target, []):
            L.append(f"- **{f_['family_id']}** 成员：{f_['members']}")
        L.append("")
    with open(os.path.join(OUT_DIR, "families.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    _log(f"[done] {OUT_DIR}/families.{{md,json}} 总 {(time.time() - T0) / 60:.0f}min")


if __name__ == "__main__":
    main()
