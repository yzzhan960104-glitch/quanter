# -*- coding: utf-8 -*-
"""R8 · 部署口径参数重优 · 8 小时三阶段循环（2026-08-27，方案见
docs/superpowers/plans/2026-08-27-r8-deployable-param-tuning.md）。

════════════════════════════════════════════════════════════════════
预登记协议（先于发射写死——此刻未看过任何部署口径参数变体读数）
════════════════════════════════════════════════════════════════════
正当性：R7c 调度保费发现=模拟器结构变化 →「冻结不搜索」解冻条款首次触发。
历史全部参数结论（B3 冠军/活跃冻结划分/不敏感判定）均系先知口径；部署口径
（random 多种子）参数响应面从未被搜索。方向预测（跑完对照）：吞吐族
（max_holding/tp_h_mult/trailing/tp_adapt）部署口径最优档位系统性偏紧。

目标（循环内）：inner 2021-24 段冻结 PM（4×7.5% 整手+min5+freeze@100w）
× queue_order=random × LOOP_SEEDS=[0,1,2,3,4] 的**配对差值中位**（CRN 共同
随机数：候选与基线同种子列表）。outer 2025-26 = holdout，只进终审不进搜索。

预登记闸（采纳需全过，两段式：筛选 G1/G2 → 采纳前全闸 G3-G5）：
  G1 主闸：median_s[ann_cand(s) − ann_base(s)] ≥ +0.02（inner deployable）；
  G2 符号一致：≥4/5 种子同向；
  G3 逐年不塌：inner 各年（21-24）deployable 中位恶化 ≤10pp；
  G4 换手守卫：inner deployable n_taken 中位 ≥ 基线 70%；
  G5 oracle 护栏：oracle 全期(2021-26) ann Δ ≥ −0.05；
Phase A 弱化晋升闸（冻结维重扫）：Δ中位 ≥ +1.5pp 且 3/3 同向（K=3）。
Phase C 终审（K=21 seeds 0-20，配对）：冠军 vs 原 B3——outer holdout 中位
Δ ≥ 0 且 ≥15/21 同向，否则整体否决（fail-closed，维持 incumbent）。

排程：Bootstrap → Phase A（≤2h，冻结 10 维重扫）→ Phase B（≤7h，贪心：
活跃 12 维 + B3 轴 2 维 + Phase A 晋升维，exec 维先扫吃识别缓存红利，
连续 2 轮零采纳提前收敛）→ Phase C（≤8h，K=21 终审+双口径全表）。
硬预算 --hours 8（默认）；各阶段窗口为上界，先到先停。

红线：C2 内核零触碰；ADR-16 池子侧不进参数；闸中途不改；DRAFT 物化留手动
（报告打印命令，脚本不写 experiments.db）。

用法（仓库根 cwd）：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/r8_deploy_param_loop.py --hours 8
"""
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

OUT_DIR = "logs/r8_deploy_loop"
STATE_PATH = os.path.join(OUT_DIR, "state.json")

LOOP_SEEDS = [0, 1, 2, 3, 4]
PHASEA_SEEDS = [0, 1, 2]
FINAL_SEEDS = list(range(21))

G1_TH = 0.02          # 配对中位 Δann 采纳阈（inner deployable）
G2_MIN = 4            # /5 种子同向
G3_MAXDROP = 0.10     # 逐年恶化上限
G4_MIN_TAKEN = 0.70   # n_taken 下限（相对基线）
G5_ORACLE_FLOOR = -0.05
A_TH, A_SIGN = 0.015, 3   # Phase A 弱化晋升闸（K=3 → 3/3 同向）
FINAL_SIGN_MIN = 15   # /21

# —— 预登记档位网格（中心=B3 ACTIVE 值；吞吐族按方向假设偏紧布档）——
PHASE_A_GRID = {   # 冻结 10 维（先知口径「不敏感/默认最优」判定的部署口径重验）
    "tp1_portion": [0.5, 0.7],
    "trailing_grace": [0, 5, 20],
    "trailing_step": [0.0, 0.10, 0.20],
    "trailing_floor": [0.0, 1.0],
    "window": [40, 80],
    "min_touches": [3],
    "min_bottoms": [3],
    "local_extrema_window": [2, 5],
    "timeout_extend_days": [3, 7],
    "timeout_extend_min_pnl": [0.02, 0.10],
}
PHASE_B_GRID = {   # 活跃维（exec 先 id 后）+ B3 轴；吞吐族偏紧
    "max_holding": [10, 15, 20, 25],
    "max_wait": [5, 10, 20],
    "cooldown": [1, 3],
    "buy_limit_atr_mult": [1.5, 2.0, 3.0],
    "tp1_h_mult": [1.0, 1.5, 3.0],
    "cancel_thresh_mult": [0.5, 1.0],
    "chase_entry": [False],
    "tp_adapt_h_atr": [2.0, 4.0, None],
    "tp_adapt_scale": [0.5, 0.8],
    "min_suppression": [0.05, 0.10, 0.25, 0.35],
    "max_h_atr": [3.0, 4.0, 5.0, 6.5],
    "stop_atr_mult": [1.0, 2.0, 2.5],
    "tp_h_mult": [0.8, 1.0, 1.2, 2.0],
    "decay_tau": [30, 60, 150],
}
ID_DIMS = {"window", "min_touches", "min_bottoms", "local_extrema_window",
           "min_suppression", "max_h_atr", "stop_atr_mult", "tp_h_mult",
           "decay_tau", "breakout_vol_mult", "min_rr"}


def _pm(seed=None, **kw):
    from backtest.models import PositionModel
    d = dict(capital=1_000_000, lot_size=100, min_fee=5.0,
             max_positions=4, pos_cap=0.075, freeze_pending=True)
    if seed is not None:
        d.update(queue_order="random", queue_seed=seed)
    d.update(kw)
    return PositionModel(**d)


def _filled_dicts(filled):
    return [{"symbol": r["symbol"], "signal_date": r["signal_date"],
             "buy_date": r["buy_date"], "exit_date": r["exit_date"],
             "avg_pnl_pct": r["avg_pnl_pct"], "entry": r.get("entry")}
            for r in filled]


def _ann(filled_dicts, seg, udates, pm):
    from discovery.objective import portfolio_metrics
    m = portfolio_metrics(filled_dicts, seg, udates, position_model=pm)
    return m


class Runner:
    def __init__(self, universe, udates, split):
        self.universe = universe
        self.udates = udates
        self.inner, self.outer = split.inner, split.outer
        self.full = _seg("full", date(2021, 1, 1), date(2026, 12, 31))
        self.n_eval = 0

    def scan(self, params):
        from discovery.objective import run_full_scan
        self.n_eval += 1
        return run_full_scan(params, self.universe)

    def screen(self, params, seeds):
        """G1/G2 筛选评估：返回 (delta_median, n_pos, cand_anns) 或 None（崩溃）。"""
        try:
            filled = _filled_dicts(self.scan(params))
        except Exception:
            print("[eval][WARN] scan 异常跳过", flush=True)
            return None
        cand = {s: _ann(filled, self.inner, self.udates, _pm(seed=s))["ann"]
                for s in seeds}
        return cand

    def full_gates(self, params, base_cache, seeds):
        """采纳前全闸：G3 逐年/G4 换手/G5 oracle。返回 (ok, detail, cand_cache)。"""
        filled = _filled_dicts(self.scan(params))
        cand_inner = {s: _ann(filled, self.inner, self.udates, _pm(seed=s))
                      for s in seeds}
        cand_cache = {"inner_anns": {s: m["ann"] for s, m in cand_inner.items()},
                      "n_taken_med": float(np.median(
                          [m["n_taken"] for m in cand_inner.values()])),
                      "yearly": {}, "oracle_full": None}
        ok = True
        for y in (2021, 2022, 2023, 2024):
            seg = _seg(f"y{y}", date(y, 1, 1), date(y, 12, 31))
            med = float(np.median([_ann(filled, seg, self.udates, _pm(seed=s))["ann"]
                                   for s in seeds]))
            cand_cache["yearly"][y] = round(med, 4)
            if base_cache["yearly"][y] - med > G3_MAXDROP:
                ok = False
        if cand_cache["n_taken_med"] < G4_MIN_TAKEN * base_cache["n_taken_med"]:
            ok = False
        o = _ann(filled, self.full, self.udates, _pm())["ann"]
        cand_cache["oracle_full"] = round(o, 4)
        if o - base_cache["oracle_full"] < G5_ORACLE_FLOOR:
            ok = False
        return ok, cand_cache


def _seg(name, start, end):
    from discovery.split import Segment
    return Segment(name, start, end)


def _save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, STATE_PATH)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8.0)
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()
    hard_end = t0 + args.hours * 3600
    phaseA_end = t0 + 2.0 * 3600
    phaseB_end = t0 + 7.0 * 3600

    from discovery.snapshot import freeze
    from discovery.split import extended_split
    from discovery.objective import run_full_scan  # noqa: F401

    print(f"[boot] freeze('2021-01-01') 加载宇宙（驻留内存，18:00 EOD 新湖无碍）...",
          flush=True)
    universe, meta = freeze("2021-01-01")
    udates = next(iter(universe.values())).index
    split = extended_split()   # inner 2021-24 / outer 2025-26
    R = Runner(universe, udates, split)
    print(f"[boot] universe={meta.universe_count} hash={meta.snapshot_hash}",
          flush=True)

    base = json.load(open("logs/r6_10_loop/state.json", encoding="utf-8"))["base"]

    # —— 基线锚：B3 incumbent 全套指标（K=21 终审用 + 循环内 K=5 缓存）——
    print("[boot] incumbent 扫描 + 基线锚 ...", flush=True)
    inc_filled = _filled_dicts(R.scan(base))
    inc_inner = {s: _ann(inc_filled, R.inner, R.udates, _pm(seed=s))["ann"]
                 for s in FINAL_SEEDS}
    inc_outer = {s: _ann(inc_filled, R.outer, R.udates, _pm(seed=s))["ann"]
                 for s in FINAL_SEEDS}
    inc_oracle_full = _ann(inc_filled, R.full, R.udates, _pm())["ann"]
    base_cache = {
        "inner_anns": {s: inc_inner[s] for s in LOOP_SEEDS},
        "n_taken_med": float(np.median([_ann(inc_filled, R.inner, R.udates,
                                             _pm(seed=s))["n_taken"]
                                        for s in LOOP_SEEDS])),
        "yearly": {y: round(float(np.median(
            [_ann(inc_filled, _seg(f"y{y}", date(y, 1, 1), date(y, 12, 31)),
                  R.udates, _pm(seed=s))["ann"] for s in LOOP_SEEDS])), 4)
            for y in (2021, 2022, 2023, 2024)},
        "oracle_full": round(inc_oracle_full, 4),
    }
    inc0 = {"params": base, "inner": inc_inner, "outer": inc_outer,
            "oracle_full": round(inc_oracle_full, 4)}
    st = {"started_at": datetime.now().isoformat(), "hours": args.hours,
          "base": base, "base_cache": base_cache, "incumbent0": inc0,
          "adopted": [], "promoted_from_A": [], "rounds_no_adopt": 0,
          "n_eval": 0, "phase": "A", "history": []}
    _save_state(st)
    print(f"[boot] 基线锚：inner deployable 中位 "
          f"{np.median(list(base_cache['inner_anns'].values())):+.3f} / "
          f"oracle 全期 {inc_oracle_full:+.3f} / n_taken "
          f"{base_cache['n_taken_med']:.0f}", flush=True)

    def try_adopt(dim, lv, base, base_cache, seeds, weak=False):
        """评估单变体并按闸裁决。返回 (adopted, new_base, new_cache, info)。"""
        cand = R.screen({**base, dim: lv}, seeds)
        if cand is None:
            return False, base, base_cache, {"dim": dim, "lv": lv, "err": True}
        deltas = {s: cand[s] - base_cache["inner_anns"][s] for s in seeds}
        d_med = float(np.median(list(deltas.values())))
        n_pos = sum(1 for v in deltas.values() if v > 0)
        th, smin = (A_TH, A_SIGN) if weak else (G1_TH, G2_MIN)
        info = {"dim": dim, "lv": lv, "d_med": round(d_med, 4),
                "n_pos": n_pos, "screen_pass": bool(d_med >= th and n_pos >= smin)}
        print(f"[eval] {dim}={lv} Δ中位{d_med:+.4f} 同向{n_pos}/{len(seeds)}"
              f"{' → 过筛' if info['screen_pass'] else ''}", flush=True)
        if weak:
            return info["screen_pass"], base, base_cache, info
        if not info["screen_pass"]:
            return False, base, base_cache, info
        ok, cand_cache = R.full_gates({**base, dim: lv}, base_cache, seeds)
        info["gates_pass"] = ok
        info["cand_cache"] = {k: v for k, v in cand_cache.items()
                              if k != "inner_anns"} if ok else None
        print(f"[gate] {dim}={lv} G3-5 {'过' if ok else '否'} "
              f"(oracle_full Δ{cand_cache['oracle_full'] - base_cache['oracle_full']:+.3f} "
              f"taken {cand_cache['n_taken_med']:.0f})", flush=True)
        if not ok:
            return False, base, base_cache, info
        return True, {**base, dim: lv}, cand_cache, info

    # ── Phase A：冻结维部署口径敏感性重扫（弱化晋升闸）──
    print(f"\n[PhaseA] 冻结 10 维重扫（K=3 配对）开始", flush=True)
    for dim, levels in PHASE_A_GRID.items():
        if time.time() > phaseA_end:
            print("[PhaseA] 时窗到，提前收", flush=True)
            break
        for lv in levels:
            prom, _, _, info = try_adopt(dim, lv, base, base_cache,
                                         PHASEA_SEEDS, weak=True)
            if prom:
                st["promoted_from_A"].append({"dim": dim, "lv": lv,
                                              "d_med": info["d_med"]})
                print(f"[PhaseA] 晋升：{dim}={lv}", flush=True)
                break
    st["n_eval"] = R.n_eval
    _save_state(st)
    print(f"[PhaseA] 完成：晋升 {len(st['promoted_from_A'])} 维 "
          f"({[p['dim'] for p in st['promoted_from_A']]}) evals={R.n_eval}",
          flush=True)

    # ── Phase B：贪心（活跃维 + 晋升维）──
    grid = dict(PHASE_B_GRID)
    for p in st["promoted_from_A"]:
        grid.setdefault(p["dim"], [])
        grid[p["dim"]] = [p["lv"]] + [x for x in grid[p["dim"]] if x != p["lv"]]
    st["phase"] = "B"
    _save_state(st)
    print(f"\n[PhaseB] 贪心开始（{len(grid)} 维，exec 先 id 后）", flush=True)
    order = sorted(grid, key=lambda d: (d in ID_DIMS,))   # exec 先（False<True）
    rnd = 0
    while time.time() < phaseB_end:
        rnd += 1
        n_adopt_round = 0
        for dim in order:
            if time.time() > phaseB_end - 150:
                break
            for lv in grid[dim]:
                if lv == base.get(dim):
                    continue
                if time.time() > phaseB_end - 150:
                    break
                adopted, base, base_cache, info = try_adopt(
                    dim, lv, base, base_cache, LOOP_SEEDS)
                if adopted:
                    n_adopt_round += 1
                    st["adopted"].append({**info, "at": datetime.now().isoformat(),
                                          "params_key": {dim: lv}})
                    st["base"], st["base_cache"] = base, base_cache
                    st["n_eval"] = R.n_eval
                    _save_state(st)
                    print(f"[ADOPT] {dim}={lv} → base 更新（累计 "
                          f"{len(st['adopted'])} 步）", flush=True)
                    break   # 每维取首个过闸档，下轮再细
        st["history"].append({"round": rnd, "n_eval": R.n_eval,
                              "n_adopt": n_adopt_round,
                              "at": datetime.now().isoformat()})
        st["rounds_no_adopt"] = 0 if n_adopt_round else st["rounds_no_adopt"] + 1
        st["n_eval"] = R.n_eval
        _save_state(st)
        print(f"[round {rnd}] 采纳 {n_adopt_round} 步 / evals={R.n_eval} / "
              f"连续零采纳 {st['rounds_no_adopt']}", flush=True)
        if st["rounds_no_adopt"] >= 2:
            print("[PhaseB] 连续 2 轮零采纳——自然收敛", flush=True)
            break
    # ── Phase C：终审（K=21 配对 vs 原 incumbent）──
    print(f"\n[PhaseC] 终审 K=21：champion vs incumbent0", flush=True)
    champ = R.scan(base)
    champ_f = _filled_dicts(champ)
    ch_inner = {s: _ann(champ_f, R.inner, R.udates, _pm(seed=s))["ann"]
                for s in FINAL_SEEDS}
    ch_outer = {s: _ann(champ_f, R.outer, R.udates, _pm(seed=s))["ann"]
                for s in FINAL_SEEDS}
    ch_oracle_full = _ann(champ_f, R.full, R.udates, _pm())["ann"]
    d_in = [ch_inner[s] - inc0["inner"][s] for s in FINAL_SEEDS]
    d_out = [ch_outer[s] - inc0["outer"][s] for s in FINAL_SEEDS]
    outer_med = float(np.median(d_out))
    outer_sign = sum(1 for v in d_out if v > 0)
    verdict = ("PASS" if (len(st["adopted"]) > 0
                          and outer_med >= 0 and outer_sign >= FINAL_SIGN_MIN)
               else "VETO" if len(st["adopted"]) > 0 else "NULL（零采纳）")
    final = {
        "verdict": verdict,
        "n_adopted": len(st["adopted"]),
        "adopted": st["adopted"],
        "champion_params": base,
        "inner": {"median_delta": round(float(np.median(d_in)), 4),
                  "sign": f"{sum(v > 0 for v in d_in)}/21",
                  "champ_med": round(float(np.median(list(ch_inner.values()))), 4),
                  "inc_med": round(float(np.median(list(inc0['inner'].values()))), 4)},
        "outer_holdout": {"median_delta": round(outer_med, 4),
                          "sign": f"{outer_sign}/21",
                          "champ_med": round(float(np.median(list(ch_outer.values()))), 4),
                          "inc_med": round(float(np.median(list(inc0['outer'].values()))), 4)},
        "oracle_full": {"champ": round(ch_oracle_full, 4),
                        "inc": inc0["oracle_full"]},
        "promoted_from_A": st["promoted_from_A"],
        "n_eval": R.n_eval,
        "took_min": round((time.time() - t0) / 60, 1),
        "completed_at": datetime.now().isoformat(),
    }
    st["final"] = final
    st["phase"] = "C"
    _save_state(st)
    with open(os.path.join(OUT_DIR, "final_report.json"), "w",
              encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps({k: final[k] for k in
                      ("verdict", "n_adopted", "inner", "outer_holdout",
                       "oracle_full", "took_min")}, ensure_ascii=False, indent=1),
          flush=True)
    if verdict == "PASS":
        print("[PhaseC] 过闸——DRAFT 物化留手动（参数在 final_report.json "
              "champion_params；promote 走 experiment CLI）", flush=True)
    elif verdict.startswith("VETO"):
        print("[PhaseC] 外层 holdout 否决——维持 incumbent（fail-closed）", flush=True)
    else:
        print("[PhaseC] 零采纳——部署口径参数不敏感亦是战役级交付（收档）", flush=True)
    print(f"[done] {OUT_DIR}/final_report.json "
          f"({(time.time() - t0) / 3600:.2f}h, evals={R.n_eval})", flush=True)


if __name__ == "__main__":
    main()
