# -*- coding: utf-8 -*-
"""R6-8 九小时自动参数优化循环（2026-08-26 用户指令「继续自动化完成9个小时」）。

起点 base = supp04+tp1_h_mult=2.0（R6-6 冠军，真值 outer_raw +115.4% / 模拟线
+99.1% / min_yr 21.5）。本循环吸收全战役教训的结构升级：

  1. R4 教训（采纳规则缺 dd/wf 约束是设计缺陷）→ 采纳闸五条：raw outer ↑≥1pp
     ∧ 模拟线 outer 不降>2pp ∧ raw dd 不恶化>2pp ∧ min_yearly_calmar（模拟线
     分年，防 2024 式塌方）不降>3.0 ∧ n_inner 不塌半（耦合6 代理）。
  2. R6-1 解耦缓存红利 → exec 维项 ~15-40s（识别全跳过）；档位扫描顺序 exec 先
     （同 id_cfg 家族缓存连击），id 维每项 ~2.5min 冷。
  3. R6-4/R6-8 前置：幽灵修复 + PARAM_SPACE 扩容（R6a 真值档）+ 撤 tp1≤tp_h
     禁区（constraints 耦合3）——TPE 首次可达新冠军所在区（R5-P3 无一超旧前沿
     的结构性原因）。
  4. 贪心立即采纳：worklist 自当前 base 生成（一次一维可归因），每采纳即重建
     worklist（剩余维自新 base 再生成，保持「当前 base+δ」语义）；每步 checkpoint。

循环：coarse 全档扫 → fine ±步长扫 → TPE（discovery CLI 子进程，budget 60/轮，
n-proc 6，min_yearly_calmar 目标）→ 循环直至 deadline−40min → 终局（FINAL 评估
+ 年表 + 物化 DRAFT + 余量≥35min 跑七门 evaluate_gates）。

用法：
    PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -u diag/r6_8_nine_hour_loop.py --hours 9
断点续跑：logs/r6_8_loop/state.json（deadline 取首启时刻，重启不重置）。
"""
import argparse
import copy
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv310/Scripts/python.exe")
LOOP_DIR = ROOT / "logs/r6_8_loop"
STATE = LOOP_DIR / "state.json"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "DISCOVERY_MANUAL_RISK": "on"}

# 预登记采纳闸（先于看数；R4 教训升级）
ADOPT_RAW_PP = 0.01        # raw outer 提升 ≥1pp
ADOPT_MR_TOL = 0.02        # 模拟线 outer 不降 >2pp
ADOPT_DD_TOL = 0.02        # raw outer dd 不恶化 >2pp
ADOPT_MINYR_TOL = 3.0      # min_yearly_calmar（模拟线分年）不降 >3.0（calmar 单位）
ADOPT_N_FLOOR = 0.5        # n_inner ≥ base 的 50%（退化 params 拒收）

# coarse 档位（自 base 生成，跳过同值；R6a 真值档 ∪ 战役已知区）
COARSE = {
    "window": [40, 80, 100, 120],
    "min_touches": [3],
    "min_suppression": [0.2, 0.3, 0.5],
    "local_extrema_window": [5, 7],
    "min_bottoms": [3, 4],
    "breakout_vol_mult": [0.5, 1.5, 2.0],
    "min_rr": [0.5, 1.0, 2.0, 2.5],
    "max_h_atr": [3.5, 4.0, 5.0, 5.5],
    "stop_atr_mult": [1.0, 2.0, 2.5],
    "tp_h_mult": [0.65, 0.7, 0.8, 1.0, 1.2, 2.0, 2.5],
    "decay_tau": [None, 60, 90],
    "max_holding": [15, 20, 25, 40],
    "max_wait": [8, 12, 20, 30],
    "cooldown": [0, 5, 8],
    "buy_limit_atr_mult": [0.5, 1.0, 2.0, 2.5, 3.0],
    "tp1_h_mult": [1.0, 1.5, 2.5, 3.0],
    "tp1_portion": [0.3, 0.5, 0.9],
    "cancel_thresh_mult": [1.0, 2.0, 3.0],
    "trailing_grace": [0, 5, 15, 20],
    "trailing_step": [0.0, 0.03, 0.1, 0.15],
    "trailing_floor": [0.0, 0.25, 0.75],
    "chase_entry": [True],           # R6-5 留档旋钮：2022 防御 alpha 线索，交采纳闸裁决
    "timeout_extend_days": [10, 20], # R6-5 已证≈无操作，留档占位（预期零采纳）
}
# fine ±步长（数值维；clamp 到 sane 区间）
FINE_STEPS = {
    "window": (20, 40, 120), "min_suppression": (0.05, 0.1, 0.8), "min_rr": (0.25, 0.5, 3.0),
    "max_h_atr": (0.25, 3.0, 6.0), "stop_atr_mult": (0.25, 0.5, 3.0),
    "tp_h_mult": (0.15, 0.5, 3.0), "buy_limit_atr_mult": (0.25, 0.5, 4.0),
    "tp1_h_mult": (0.25, 0.5, 4.0), "tp1_portion": (0.2, 0.1, 0.9),
    "max_holding": (5, 10, 60), "max_wait": (3, 3, 30), "cooldown": (2, 0, 10),
    "trailing_grace": (5, 0, 30), "trailing_step": (0.03, 0.0, 0.2),
    "trailing_floor": (0.25, 0.0, 1.0), "breakout_vol_mult": (0.5, 0.3, 2.5),
    "cancel_thresh_mult": (1.0, 1.0, 4.0), "decay_tau": (30, 10, 120),
    "local_extrema_window": (2, 3, 9), "min_touches": (1, 2, 5), "min_bottoms": (1, 2, 5),
}


def _load_base() -> dict:
    p = json.loads((ROOT / "logs/r4_loop/state.json").read_text(encoding="utf-8"))["base"]
    return {**p, "min_suppression": 0.4, "tp1_h_mult": 2.0}


_cache: dict = {}


def _ensure_universe():
    if "universe" not in _cache:
        from discovery.snapshot import freeze
        from discovery.split import holdout_split
        from discovery.manual_risk_sim import build_block_calendar
        _cache["universe"], _cache["meta"] = freeze("2021-01-01")
        _cache["split"] = holdout_split()
        _cache["cal"] = build_block_calendar(_cache["universe"])
    return _cache


def _eval_one(params: dict, tag: str) -> dict:
    """双口径评估（组合口径键 ann/max_dd/n；R6-1 缓存自动生效于 scan_symbol 内）。"""
    from discovery.objective import evaluate_portfolio
    from strategies.neckline import backtest as bk
    c = _ensure_universe()
    res = evaluate_portfolio(params, c["universe"], c["split"], block_dates=c["cal"])
    out = {"tag": tag,
           "raw_outer_ann": res["outer_raw"]["ann"],
           "raw_outer_dd": res["outer_raw"]["max_dd"],
           "mr_outer_ann": res["outer"]["ann"],
           "inner_ann": res["inner"]["ann"],
           "min_yr": res["inner"]["min_yearly_calmar"],
           "n_inner": res["inner"]["n"]}
    print("    [{:>30}] raw {:+7.1%} dd {:4.0%} | 模拟线 {:+7.1%} min_yr {:6.1f} | n={}"
          .format(tag, out["raw_outer_ann"], out["raw_outer_dd"], out["mr_outer_ann"],
                  out["min_yr"], out["n_inner"]), flush=True)
    return out


def _adoptable(base_res: dict, cand: dict) -> bool:
    """预登记五条采纳闸（R4 教训：dd/年段约束先于看数写死在代码里）。"""
    return (cand["raw_outer_ann"] > base_res["raw_outer_ann"] + ADOPT_RAW_PP
            and cand["mr_outer_ann"] >= base_res["mr_outer_ann"] - ADOPT_MR_TOL
            and cand["raw_outer_dd"] <= base_res["raw_outer_dd"] + ADOPT_DD_TOL
            and cand["min_yr"] >= base_res["min_yr"] - ADOPT_MINYR_TOL
            and cand["n_inner"] >= ADOPT_N_FLOOR * base_res["n_inner"])


def _worklist(base: dict, levels_map: dict, done_keys: set) -> list:
    """自当前 base 生成 (dim, lv) 工作表：exec 维在前（缓存连击），跳过同值。"""
    exec_dims = ["max_holding", "max_wait", "cooldown", "buy_limit_atr_mult",
                 "tp1_h_mult", "tp1_portion", "cancel_thresh_mult",
                 "trailing_grace", "trailing_step", "trailing_floor",
                 "chase_entry", "timeout_extend_days"]
    order = exec_dims + [d for d in levels_map if d not in exec_dims]
    items = []
    for dim in order:
        if dim not in levels_map or dim in done_keys:
            continue
        for lv in levels_map[dim]:
            if base.get(dim) != lv:
                items.append((dim, lv))
    return items


def _fine_levels(base: dict, dim: str) -> list:
    """数值维 ±step（clamp sane 区间，去同值）。None 值维只对 decay/cancel 给探索档。"""
    if dim not in FINE_STEPS:
        return []
    step, lo, hi = FINE_STEPS[dim]
    v = base.get(dim)
    if v is None:
        return {("decay_tau"): [30, 60], "cancel_thresh_mult": [2.0, 3.0]}.get(dim, [])
    out = []
    for d in (-step, step):
        lv = round(v + d, 4)
        if isinstance(v, int) and isinstance(step, int):
            lv = int(lv)
        if lo <= lv <= hi and lv != v:
            out.append(lv)
    return out


def _sweep(base: dict, base_res: dict, mode: str, deadline: float, st: dict) -> tuple:
    """一轮贪心扫（coarse 全档 / fine ±步长），立即采纳 + 重建工作表。"""
    n_eval, n_adopt = 0, 0
    done_dims_this_sweep: set = set()
    while time.time() < deadline - 40 * 60:
        if mode == "coarse":
            items = _worklist(base, COARSE, done_dims_this_sweep)
        else:
            fl = {d: _fine_levels(base, d) for d in FINE_STEPS}
            fl = {d: lv for d, lv in fl.items() if lv}
            items = _worklist(base, fl, done_dims_this_sweep)
        # 每维一次只试最优一个？——顺序扫全部档，先过闸先得（贪心）
        if not items:
            break
        progressed = False
        for dim, lv in items:
            if time.time() > deadline - 40 * 60:
                break
            try:
                p = copy.deepcopy(base)
                p[dim] = lv
                cand = _eval_one(p, f"{mode}:{dim}={lv}")
                n_eval += 1
                if _adoptable(base_res, cand):
                    print("  ★ 采纳 {}: {} ⇒ {}（raw {:+.1%}⇒{:+.1%} min_yr {:.1f}⇒{:.1f}）"
                          .format(mode, base.get(dim), lv, base_res["raw_outer_ann"],
                                  cand["raw_outer_ann"], base_res["min_yr"], cand["min_yr"]),
                          flush=True)
                    base[dim] = lv
                    base_res = cand
                    st["adopted"].append({"mode": mode, "dim": dim, "lv": lv,
                                          "raw_outer": cand["raw_outer_ann"],
                                          "mr_outer": cand["mr_outer_ann"],
                                          "min_yr": cand["min_yr"],
                                          "at": datetime.now().isoformat(timespec="seconds")})
                    st["base"] = base
                    st["base_res"] = {k: v for k, v in base_res.items() if k != "tag"}
                    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
                    n_adopt += 1
                    progressed = True
                    done_dims_this_sweep = set()   # base 变了：全维重开（δ 自新 base）
                    break                            # 重建工作表
            except Exception as e:
                print("    [{}:{}] 评估异常跳过：{}: {}".format(mode, dim, type(e).__name__, e),
                      flush=True)
        if not progressed:
            break   # 整表扫完零采纳 → 本模式收敛
    return base, base_res, n_eval, n_adopt


def _tpe_round(base: dict, base_res: dict, rnd: int, deadline: float, st: dict):
    """TPE 子进程（discovery CLI，扩容后空间含新冠军区）→ top 受控复核采纳。"""
    log = LOOP_DIR / f"tpe_r{rnd}.log"
    print(f"  [TPE r{rnd}] discovery run budget=60 → {log.name}", flush=True)
    try:
        with log.open("w", encoding="utf-8") as f:
            subprocess.run([PY, "-u", "-m", "discovery", "run", "--budget", "60",
                            "--tpe-trials", "45", "--n-proc", "6",
                            "--seed", f"20260826{rnd:02d}"],
                           stdout=f, stderr=subprocess.STDOUT, cwd=str(ROOT),
                           env=ENV, timeout=max(600, int(deadline - time.time()) - 45 * 60))
    except Exception as e:
        print(f"  [TPE r{rnd}] 子进程异常（续）：{type(e).__name__}: {e}", flush=True)
        return base, base_res, 0
    try:
        con = sqlite3.connect(f"file:{ROOT/'logs/discovery_trials.db'}?mode=ro", uri=True)
        snap = con.execute(
            "SELECT snapshot_hash FROM snapshot ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        rows = list(con.execute(
            "SELECT trial_id, params, inner_metrics, outer_metrics FROM trial "
            "WHERE snapshot_hash=? ORDER BY rowid DESC LIMIT 60", (snap,)))
        con.close()
    except Exception as e:
        print(f"  [TPE r{rnd}] 读 trial 库异常（续）：{type(e).__name__}: {e}", flush=True)
        return base, base_res, 0
    best = None
    for tid, params_s, inner_s, outer_s in rows:
        p, i, o = json.loads(params_s), json.loads(inner_s), json.loads(outer_s)
        if best is None or i.get("min_yearly_calmar", -9) > best[1]:
            best = (p, i.get("min_yearly_calmar", -9), o.get("ann"), tid, i.get("n"))
    if not best or best[3] is None:
        print("  [TPE] 无可用 trial", flush=True)
        return base, base_res, 0
    print(f"  [TPE r{rnd}] top {best[3][:12]} min_yr {best[1]:.2f} mr_outer {best[2]:+.1%}",
          flush=True)
    try:
        cand = _eval_one(best[0], f"TPE@{best[3][:8]}")
        if _adoptable(base_res, cand):
            print("  ★ TPE 采纳：raw {:+.1%}⇒{:+.1%}".format(
                base_res["raw_outer_ann"], cand["raw_outer_ann"]), flush=True)
            st["adopted"].append({"mode": "tpe", "dim": "TPE", "lv": best[3],
                                  "raw_outer": cand["raw_outer_ann"],
                                  "at": datetime.now().isoformat(timespec="seconds")})
            st["base"] = best[0]
            st["base_res"] = {k: v for k, v in cand.items() if k != "tag"}
            STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
            return best[0], cand, 1
        print("  ✗ TPE top 复核未过闸（raw {:+.1%}）".format(cand["raw_outer_ann"]), flush=True)
    except Exception as e:
        print(f"  [TPE] 复核异常（续）：{type(e).__name__}: {e}", flush=True)
    return base, base_res, 0


def _final_phase(base: dict, st: dict, deadline: float):
    """终局：FINAL 评估 + 年表 + DRAFT 物化 + 余量足则七门。"""
    from discovery.objective import run_full_scan, portfolio_metrics
    from discovery.split import Segment
    from datetime import date
    import pandas as pd
    c = _ensure_universe()
    fin = _eval_one(copy.deepcopy(base), "FINAL")
    filled = run_full_scan(base, c["universe"])
    udates = next(iter(c["universe"].values())).index
    years = {}
    for y in (2022, 2023, 2024, 2025, 2026):
        seg = Segment(f"y{y}", date(y, 1, 1), date(y, 12, 31))
        raw_m = portfolio_metrics(filled, seg, udates)
        mr_m = portfolio_metrics(filled, seg, udates, block_dates=c["cal"])
        years[y] = {"raw": raw_m["ann"], "mr": mr_m["ann"]}
    st["final"] = {k: v for k, v in fin.items() if k != "tag"}
    st["final_years"] = {str(k): v for k, v in years.items()}
    # 物化 DRAFT（幂等；不 promote）
    try:
        from research.proposals import _create_experiment_draft
        exp_id = _create_experiment_draft(base, source="r6_8_loop")
        st["final_draft_id"] = exp_id
        print(f"[final] DRAFT 已物化：{exp_id}", flush=True)
        if deadline - time.time() >= 35 * 60:
            from research.autopromote import evaluate_gates
            gates = evaluate_gates(exp_id)
            st["final_gates"] = {k: v for k, v in gates.items() if k != "gates"}
            st["final_gates_detail"] = gates["gates"]
            print(f"[final] 七门 all_pass={gates['all_pass']}", flush=True)
            for k, g in gates["gates"].items():
                if k != "_meta":
                    print(f"    [{'✓' if g['pass'] else '✗'}] {k}", flush=True)
    except Exception as e:
        print(f"[final] DRAFT/七门异常（不影响循环产物）：{type(e).__name__}: {e}", flush=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=9.0)
    args = ap.parse_args()
    LOOP_DIR.mkdir(parents=True, exist_ok=True)

    if STATE.exists():
        st = json.loads(STATE.read_text(encoding="utf-8"))
        deadline = st["deadline"]
        print(f"[loop] 断点续跑 round={st.get('round')} 采纳链 {len(st.get('adopted', []))} 步",
              flush=True)
    else:
        deadline = time.time() + args.hours * 3600
        st = {"started_at": datetime.now().isoformat(timespec="seconds"),
              "deadline": deadline, "round": 0, "base": _load_base(),
              "adopted": [], "history": []}
    base = st["base"]

    print(f"[loop] base=supp04+tp1=2.0；deadline {args.hours}h 后"
          f"（剩 {(deadline-time.time())/3600:.1f}h）", flush=True)
    base_res = _eval_one(copy.deepcopy(base), "BASE")

    while time.time() < deadline - 40 * 60:
        st["round"] += 1
        rnd = st["round"]
        t0 = time.time()
        left_h = (deadline - time.time()) / 3600
        print(f"\n===== Round {rnd}（剩 {left_h:.1f}h）=====", flush=True)
        n_eval = n_adopt = 0
        for mode in ("coarse", "fine"):
            if time.time() >= deadline - 40 * 60:
                break
            base, base_res, ev, ad = _sweep(base, base_res, mode, deadline, st)
            n_eval += ev
            n_adopt += ad
        # TPE（需 ≥2h 余量：子进程 60 budget ~1h + 复核）
        if time.time() < deadline - 2 * 3600:
            base, base_res, ad = _tpe_round(base, base_res, rnd, deadline, st)
            n_adopt += ad
        st["history"].append({"round": rnd, "took_min": round((time.time() - t0) / 60),
                              "n_eval": n_eval, "n_adopt": n_adopt,
                              "at": datetime.now().isoformat(timespec="seconds")})
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  [checkpoint] r{rnd} 用 {(time.time()-t0)/60:.0f}min "
              f"(eval {n_eval} / 采纳 {n_adopt})", flush=True)

    print("\n[loop] 进入终局阶段", flush=True)
    _final_phase(base, st, deadline)
    print(f"\n[loop] 完成：采纳链 {len(st['adopted'])} 步 → {STATE}", flush=True)
    for a in st["adopted"]:
        print("    {} {} ⇒ {} (raw {:+.1%})".format(
            a["mode"], a["dim"], a["lv"], a["raw_outer"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
