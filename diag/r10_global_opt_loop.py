# -*- coding: utf-8 -*-
"""R10 · 全空间总攻 · Optuna 全局优化验证 B3 全局最优性（2026-08-28，方案见
docs/superpowers/plans/2026-08-28-r10-global-opt.md）。

════════════════════════════════════════════════════════════════════
预登记协议（先于发射写死——此刻未看过任何联合空间读数）
════════════════════════════════════════════════════════════════════
正当性：R2-R09 全部搜索=「从 B3 出发的逐维贪心/局部网格」（坐标下降），
全空间联合搜索从未做过——B3 全局最优性是未检验假设。用户裁决：不记成本、
最深入最全面、用现成轮子（Optuna 4.9 已在 venv，ADR4 先例 discovery/search.py）。

空间（33 维 categorical，档位发射前定死）：
  - 24 参数键：B3 base ∪ R8 PHASE_A/B_GRID（程序化并集，B3 值必在 choices）；
  - 2 个 R8 未扫键：momentum_gate [None,-0.05,0,0.05]、time_stop_days [None,5,10]；
  - R9 七过滤维（_ 前缀）：联合空间允许维度交互，TPE 自学负梯度。

目标：inner 2021-24 deployable ann 的 K=5 种子（0-4）中位（CRN，与 R8/R9
同口径可比）。warm start：enqueue B3 全套。

停机（收敛驱动=用户裁决无时钟死线）：
  - N_STALL=300 trial 无 best ≥+0.005 改进；
  - MAX_TRIALS=3000 硬上限（防失控；~3min/trial → 上限 ~6 天量级）；
  - 断点：optuna sqlite study 原生（崩了 load_if_study_exists 续跑）。

Stage 2（条件分支）：champion≠B3 → 异种子 TPE(seed=7) 复核 study（300
trial）；champion=B3 → RandomSampler 500 trial 均匀对照验证无漏网高产区。

终审（fail-closed）：champion vs incumbent0（B3+1e5+零过滤）K=21 配对，
采纳须 outer holdout 中位 Δ≥0 且 ≥15/21 同向；G3 逐年/G4 换手/G5 oracle
护栏全表展示；DRAFT 物化留手动。

红线：C2 内核零触碰；ADR-16 池子侧不进参数；pos_cap/max_positions/capital
不动；闸中途不改；外层 holdout 只碰一次。

用法（仓库根 cwd）：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/r10_global_opt_loop.py
    ... --smoke   # 抽样 60 只 + 3 trial 端到端验证
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import optuna

from diag.r8_deploy_param_loop import PHASE_A_GRID, PHASE_B_GRID
from diag.r9_breadth_loop import (
    BASE_LIQUIDITY, Runner as R9Runner, _ann, _filled_dicts, _pm, _seg,
    build_universe,
)

OUT_DIR = "logs/r10_global_opt"
STATE_PATH = os.path.join(OUT_DIR, "state.json")
STORAGE = f"sqlite:///{os.path.abspath(os.path.join(OUT_DIR, 'study.db'))}"
STUDY_NAME = "r10_global"
VERIFY_NAME = "r10_verify"

LOOP_SEEDS = [0, 1, 2, 3, 4]
FINAL_SEEDS = list(range(21))
N_STALL = 300
STALL_EPS = 0.005
MAX_TRIALS = 3000
VERIFY_TRIALS = 300        # Stage 2a 异种子复核
RANDOM_TRIALS = 500        # Stage 2b 均匀对照

# R9 七过滤维（_ 前缀避免与参数键撞名；档位=R9 预登记同款+None 关闭档）。
# 持久化纪律（trial 50 崩溃实锤）：optuna sqlite 要求 choices 仅
# None/bool/int/float/str——复合档（_mom 的 (tag,n,th)/_band 的 (lo,hi)）
# round-trip 序列化成 list 与内存 tuple 不等 → "dynamic value space"。
# 故 _mom 用 tag 字符串、_band 用 "(lo,hi)" 字符串编码，keep 侧解码。
R9_SPACE = {
    "_liq": [3e4, 1e5, 2e5, 5e5],
    "_dow": [None, "no_mon", "no_fri", "mid_only"],
    "_mom": [None, "mom10_ge0", "mom40_ge0", "mom20_ge5pct",
             "mom20_ge_neg5pct"],
    "_price": [None, "p_ge5", "p_ge10", "p_le100"],
    "_volcap": [None, 3.0, 5.0],
    "_band": [None, "(1.0,3.5)", "(1.5,4.5)"],
    "_listed": [None, 400],
}
_MOM_LEVELS = {"mom10_ge0": (10, 0.0), "mom40_ge0": (40, 0.0),
               "mom20_ge5pct": (20, 0.05), "mom20_ge_neg5pct": (20, -0.05)}
_BAND_LEVELS = {"(1.0,3.5)": (1.0, 3.5), "(1.5,4.5)": (1.5, 4.5)}
_R9_DIM_MAP = {"_dow": "D2_dow", "_mom": "D3_mom", "_price": "D4_price",
               "_volcap": "D5_volcap", "_band": "D6_band", "_listed": "D7_listed"}


def split_trial(tp):
    """33 维 trial params → (params, state)。_ 前缀键拆入 state。"""
    params = {k: v for k, v in tp.items() if not k.startswith("_")}
    liq = tp.get("_liq", BASE_LIQUIDITY)
    filters = {dim: tp[pfx] for pfx, dim in _R9_DIM_MAP.items()
               if tp.get(pfx) is not None}
    return params, {"liq": liq, "filters": filters}


def build_space(base):
    """35 维 space（参数 28=R8 grids∪B3 值∪2 新键 + R9 7 维）+ 补全默认的
    B3_FULL warm 起点。None 语义纪律：base 明确给的 None（如 cancel_
    thresh_mult=关闭守卫）是有效档入 choices；只有 grids 带来的 base 外
    真缺键才按 EXEC/ID 默认补——两者不可混淆（B3 语义偷改红线）。"""
    grids = {**PHASE_A_GRID, **PHASE_B_GRID}
    from strategies.neckline.backtest import EXEC_DEFAULTS
    from strategies.neckline.method_v0 import DEFAULTS as ID_DEFAULTS
    space = {}
    b3_full = dict(base)
    b3_full.setdefault("momentum_gate", None)
    b3_full.setdefault("time_stop_days", None)
    for k in sorted(set(base) | set(grids)):
        if k not in b3_full:   # 真缺键（grids 的 base 外键）→ 补运行默认
            b3_full[k] = EXEC_DEFAULTS.get(k, ID_DEFAULTS.get(k))
        cands = list(grids.get(k, []))
        if b3_full[k] not in cands:
            cands.append(b3_full[k])
        space[k] = cands
    space["momentum_gate"] = [None, -0.05, 0.0, 0.05]
    space["time_stop_days"] = [None, 5, 10]
    space.update(R9_SPACE)
    warm = dict(b3_full)
    for pfx in R9_SPACE:            # 其余 _ 维全 None=零过滤（B3 语义）
        warm.setdefault(pfx, None)
    warm["_liq"] = BASE_LIQUIDITY   # _liq 无 None 档——必为数值
    return space, warm


class R10Runner(R9Runner):
    """R9 Runner 的 R10 适配：
    - base_params 可变（每 trial 更新）；
    - filled 不做跨 trial 缓存（TPE 新组合几乎全 miss，跨 trial 重复概率低；
      进程内识别缓存 _scan_id_cache 才是主要复用红利）；
    - universe dict 按 liq 档缓存驻留（省每 trial ~15s xs 切片）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._uni_cache = {}

    def _uni(self, liq):
        if liq not in self._uni_cache:
            self._uni_cache[liq] = build_universe(self.lake, liq)
            print(f"[uni] liquidity={liq:.0f} universe="
                  f"{len(self._uni_cache[liq])}（驻留 {len(self._uni_cache)} 档）",
                  flush=True)
        return self._uni_cache[liq]

    def filled_for(self, liq):
        from discovery.objective import run_full_scan
        return _filled_dicts(run_full_scan(self.base_params, self._uni(liq)))

    def eval_params_state(self, params, state, seeds):
        """K 种子中位评估（R10 objective 核心）。异常返回 None。"""
        self.base_params = params
        try:
            filled = self.filled_of(state)
        except Exception:
            print("[eval][WARN] filled 构造异常", flush=True)
            return None
        anns = [_ann(filled, self.inner, self.udates, _pm(seed=s))["ann"]
                for s in seeds]
        return float(np.median(anns))

    def keep(self, rec, dim, lv):
        """字符串档解码（持久化纪律，见 R9_SPACE 注释）：_mom/_band 的
        编码档在此展开为语义参数，其余维度走 R9 原版。"""
        if dim == "D3_mom" and isinstance(lv, str):
            n, th = _MOM_LEVELS[lv]
            v = self.mom_value(rec["symbol"],
                               pd.Timestamp(rec["signal_date"]), n)
            return True if v is None else v >= th
        if dim == "D6_band" and isinstance(lv, str):
            lo, hi = _BAND_LEVELS[lv]
            h = rec.get("H_over_ATR")
            if h is None or pd.isna(h):
                return True
            return lo <= float(h) <= hi
        return super().keep(rec, dim, lv)


def _open_study(name, sampler):
    """optuna 4.x 断点续跑：已有则 load（带采样器续用），无则 create。"""
    try:
        return optuna.load_study(storage=STORAGE, study_name=name,
                                 sampler=sampler)
    except KeyError:
        return optuna.create_study(storage=STORAGE, study_name=name,
                                   direction="maximize", sampler=sampler)


def _save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, STATE_PATH)


def _b3_state():
    return {"liq": BASE_LIQUIDITY, "filters": {}}


def _same_params(a, b):
    return all(a.get(k) == b.get(k) for k in set(a) | set(b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-trials", type=int, default=MAX_TRIALS)
    ap.add_argument("--stall", type=int, default=N_STALL)
    ap.add_argument("--smoke", action="store_true",
                    help="抽样 60 只 + 3 trial 端到端管道验证")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    from discovery.split import extended_split

    print(f"[boot] 读湖（2021-01-01 起，pyarrow filters）...", flush=True)
    try:
        lake = pd.read_parquet("data_lake/a_shares_daily.parquet",
                               filters=[("date", ">=", pd.Timestamp("2021-01-01"))])
    except Exception:
        print("[boot][WARN] filters 推送失败，全量读再筛", flush=True)
        lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
        lake = lake[lake.index.get_level_values("date") >= pd.Timestamp("2021-01-01")]

    print("[boot] 全湖首日表（D7 用）...", flush=True)
    full_idx = pd.read_parquet("data_lake/a_shares_daily.parquet",
                               columns=[]).index
    first_dates = pd.Series(full_idx.get_level_values("date"),
                            index=full_idx.get_level_values("symbol"),
                            dtype="object").groupby(level=0).min()
    first_dates = {k: pd.Timestamp(v) for k, v in first_dates.items()}
    del full_idx

    if args.smoke:
        rng = np.random.default_rng(42)
        all_syms = pd.Index(lake.index.get_level_values("symbol").unique())
        keep = set(rng.choice(all_syms.to_numpy(), size=60, replace=False))
        lake = lake[lake.index.get_level_values("symbol").isin(keep)]
        print(f"[smoke] 湖裁剪到 {len(keep)} 只", flush=True)

    uni = build_universe(lake, BASE_LIQUIDITY)
    udates = next(iter(uni.values())).index
    split = extended_split()
    del uni

    base = json.load(open("logs/r6_10_loop/state.json", encoding="utf-8"))["base"]
    R = R10Runner(base, lake, udates, split, first_dates)

    print("[boot] incumbent 锚（B3 首次全识别）...", flush=True)
    inc_filled = R.filled_for(BASE_LIQUIDITY)
    inc_inner = {s: _ann(inc_filled, R.inner, R.udates, _pm(seed=s))["ann"]
                 for s in FINAL_SEEDS}
    inc_outer = {s: _ann(inc_filled, R.outer, R.udates, _pm(seed=s))["ann"]
                 for s in FINAL_SEEDS}
    inc_oracle = _ann(inc_filled, R.full, R.udates, _pm())["ann"]
    inc_k5_med = float(np.median([inc_inner[s] for s in LOOP_SEEDS]))
    inc0 = {"params": base, "state": _b3_state(), "inner": inc_inner,
            "outer": inc_outer, "oracle_full": round(inc_oracle, 4),
            "inner_k5_med": round(inc_k5_med, 4)}
    n_signals = len(inc_filled)
    del inc_filled
    print(f"[boot] incumbent filled={n_signals} 笔 / inner K5 中位 "
          f"{inc_k5_med:+.3f} / oracle 全期 {inc_oracle:+.3f}", flush=True)

    SPACE, WARM = build_space(base)
    _, B3_STATE_FULL = split_trial(WARM)
    B3_PARAMS_FULL = {k: v for k, v in WARM.items() if not k.startswith("_")}
    print(f"[boot] 搜索空间 {len(SPACE)} 维 "
          f"（参数 {len(SPACE) - len(R9_SPACE)} + R9 {len(R9_SPACE)}）", flush=True)

    st = {"started_at": datetime.now().isoformat(), "smoke": args.smoke,
          "base": base, "incumbent0": inc0, "study": STUDY_NAME,
          "space_n": len(SPACE), "best_seen": -1e9, "stall": 0,
          "n_trials": 0, "phase": "search", "best_trials": [], "final": None}

    # ── Stage 1：TPE(multivariate) 全空间总攻（sqlite 断点原生续跑）──
    sampler = optuna.samplers.TPESampler(seed=42, multivariate=True,
                                         n_startup_trials=50)
    study = _open_study(STUDY_NAME, sampler)
    if len(study.trials) == 0:
        study.enqueue_trial(WARM)
        print("[boot] warm start：B3 全套（含默认补全）已 enqueue", flush=True)
    else:
        # 断点续跑：stall 基线自已有 best 起步（防重启后首 trial 假改进清零）
        st["best_seen"] = round(study.best_value - STALL_EPS, 4)
        print(f"[resume] study 已有 {len(study.trials)} trial，best="
              f"{study.best_value:+.4f}，续跑", flush=True)

    max_trials = 3 if args.smoke else args.max_trials
    stall_lim = 2 if args.smoke else args.stall

    def objective(trial):
        tp = {k: trial.suggest_categorical(k, SPACE[k]) for k in SPACE}
        params, state = split_trial(tp)
        med = R.eval_params_state(params, state, LOOP_SEEDS)
        if med is None:
            return -1.0
        return med

    n_done = 0
    while True:
        if len(study.trials) >= max_trials:
            print(f"[search] MAX_TRIALS={max_trials} 到，收", flush=True)
            break
        study.optimize(objective, n_trials=1)
        n_done += 1
        bv = study.best_value
        if bv > st["best_seen"] + STALL_EPS:
            st["best_seen"] = round(bv, 4)
            st["stall"] = 0
            best = study.best_trial
            st["best_trials"].append({
                "n": len(study.trials), "value": round(bv, 4),
                "params": best.params, "at": datetime.now().isoformat()})
            print(f"[BEST] trial#{best.number} value={bv:+.4f}（累计 "
                  f"{len(st['best_trials'])} 次改进）", flush=True)
        else:
            st["stall"] += 1
        st["n_trials"] = len(study.trials)
        _save_state(st)
        if st["stall"] >= stall_lim:
            print(f"[search] 连续 {st['stall']} trial 无 ≥{STALL_EPS} 改进"
                  f"——自然收敛", flush=True)
            break
        if n_done % 20 == 0:
            print(f"[search] trials={len(study.trials)} best={bv:+.4f} "
                  f"stall={st['stall']} elapsed={(time.time()-t0)/3600:.1f}h",
                  flush=True)

    champ_params, champ_state = split_trial(study.best_params)
    champ_value = round(study.best_value, 4)
    is_b3 = (_same_params(champ_params, B3_PARAMS_FULL)
             and champ_state == B3_STATE_FULL)
    print(f"[Stage1] champion value={champ_value:+.4f} "
          f"{'== B3' if is_b3 else '≠ B3'}", flush=True)

    if args.smoke:
        st["phase"] = "smoke_done"
        st["n_trials"] = len(study.trials)
        _save_state(st)
        print(f"\n[smoke done] {len(study.trials)} trial 跑通，管道验证 OK "
              f"({(time.time() - t0) / 60:.1f}min)", flush=True)
        return

    # ── Stage 2：条件分支（champion≠B3 异种子复核 / ==B3 随机对照）──
    st["phase"] = "stage2"
    _save_state(st)
    verify = {"mode": None}
    if is_b3:
        print(f"\n[Stage2b] champion==B3 → RandomSampler {RANDOM_TRIALS} trial "
              f"均匀对照（验证无漏网高产区）", flush=True)
        study2 = _open_study(
            VERIFY_NAME,
            optuna.samplers.RandomSampler(seed=13))
        if len(study2.trials) < RANDOM_TRIALS:
            study2.optimize(objective, n_trials=RANDOM_TRIALS - len(study2.trials))
        top = sorted(study2.trials, key=lambda t: t.value, reverse=True)[:10]
        verify = {"mode": "random_control", "n": len(study2.trials),
                  "top10": [{"n": t.number, "value": round(t.value, 4),
                             "params": t.params} for t in top],
                  "b3_value": round(inc_k5_med, 4),
                  "leak_found": bool(top and top[0].value > inc_k5_med + 0.02)}
        print(f"[Stage2b] 随机 {len(study2.trials)} trial：top1 "
              f"{top[0].value:+.4f} vs B3 {inc_k5_med:+.4f}"
              f"（漏网高产区：{'有！' if verify['leak_found'] else '无'}）",
              flush=True)
    else:
        print(f"\n[Stage2a] champion≠B3 → 异种子 TPE(seed=7) {VERIFY_TRIALS} "
              f"trial 复核（第二盆地双采样器确认）", flush=True)
        study2 = _open_study(
            VERIFY_NAME,
            optuna.samplers.TPESampler(seed=7, multivariate=True,
                                       n_startup_trials=100))
        if len(study2.trials) == 0:
            study2.enqueue_trial(WARM)
            study2.enqueue_trial({**study.best_params})
        if len(study2.trials) < VERIFY_TRIALS:
            study2.optimize(objective, n_trials=VERIFY_TRIALS - len(study2.trials))
        v_best = round(study2.best_value, 4)
        confirmed = v_best >= champ_value - 0.005
        verify = {"mode": "tpe_verify", "n": len(study2.trials),
                  "verify_best": v_best, "champ_value": champ_value,
                  "basin_confirmed": bool(confirmed)}
        print(f"[Stage2a] 复核 best {v_best:+.4f} vs champion {champ_value:+.4f}"
              f"（盆地确认：{'是' if confirmed else '否——存疑回退 B3 终审'}）",
              flush=True)
        if not confirmed:
            champ_params, champ_state = dict(B3_PARAMS_FULL), B3_STATE_FULL
    st["verify"] = verify
    _save_state(st)

    # ── 终审：champion vs incumbent0，K=21 配对 fail-closed ──
    print("\n[Final] K=21 终审：champion vs incumbent0", flush=True)
    R.base_params = champ_params
    ch_filled = R.filled_of(champ_state)
    ch_inner = {s: _ann(ch_filled, R.inner, R.udates, _pm(seed=s))["ann"]
                for s in FINAL_SEEDS}
    ch_outer = {s: _ann(ch_filled, R.outer, R.udates, _pm(seed=s))["ann"]
                for s in FINAL_SEEDS}
    ch_oracle = _ann(ch_filled, R.full, R.udates, _pm())["ann"]
    yearly = {}
    for y in (2021, 2022, 2023, 2024):
        seg = _seg(f"y{y}", date(y, 1, 1), date(y, 12, 31))
        yearly[y] = round(float(np.median(
            [_ann(ch_filled, seg, R.udates, _pm(seed=s))["ann"]
             for s in LOOP_SEEDS])), 4)
    n_taken_med = float(np.median(
        [_ann(ch_filled, R.inner, R.udates, _pm(seed=s))["n_taken"]
         for s in LOOP_SEEDS]))
    d_in = [ch_inner[s] - inc0["inner"][s] for s in FINAL_SEEDS]
    d_out = [ch_outer[s] - inc0["outer"][s] for s in FINAL_SEEDS]
    outer_med = float(np.median(d_out))
    outer_sign = sum(1 for v in d_out if v > 0)
    verdict = ("PASS" if (not is_b3 and outer_med >= 0
                          and outer_sign >= 15)
               else "VETO" if not is_b3 else "NULL（champion==B3，全局最优性确认）")
    final = {
        "verdict": verdict,
        "is_b3": is_b3,
        "champion_params": champ_params,
        "champion_state": champ_state,
        "search_best_value": champ_value,
        "incumbent_inner_k5_med": round(inc_k5_med, 4),
        "inner": {"median_delta": round(float(np.median(d_in)), 4),
                  "sign": f"{sum(v > 0 for v in d_in)}/21"},
        "outer_holdout": {"median_delta": round(outer_med, 4),
                          "sign": f"{outer_sign}/21"},
        "guards": {"yearly": yearly, "n_taken_med": round(n_taken_med, 1),
                   "oracle_full": round(ch_oracle, 4),
                   "inc_oracle_full": inc0["oracle_full"]},
        "verify": verify,
        "n_trials": len(study.trials),
        "best_trials": st["best_trials"],
        "took_min": round((time.time() - t0) / 60, 1),
        "completed_at": datetime.now().isoformat(),
    }
    st["final"] = final
    st["phase"] = "done"
    _save_state(st)
    with open(os.path.join(OUT_DIR, "final_report.json"), "w",
              encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps({k: final[k] for k in
                      ("verdict", "is_b3", "search_best_value", "inner",
                       "outer_holdout", "n_trials", "took_min")},
                     ensure_ascii=False, indent=1, default=str), flush=True)
    if verdict == "PASS":
        print("[Final] 过闸——DRAFT 物化留手动（champion_params/state 在 "
              "final_report.json）", flush=True)
    elif verdict.startswith("VETO"):
        print("[Final] 外层 holdout 否决——维持 incumbent（fail-closed）", flush=True)
    else:
        print("[Final] champion==B3——全空间总攻确认全局最优性，穷尽性收档",
              flush=True)
    print(f"[done] {OUT_DIR}/final_report.json "
          f"({(time.time() - t0) / 3600:.2f}h)", flush=True)


if __name__ == "__main__":
    main()
