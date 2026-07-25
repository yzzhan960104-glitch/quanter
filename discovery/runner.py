# -*- coding: utf-8 -*-
"""L2 跑批调度（spec §4.3 / §5.1 / §8 拷问②，断点续跑去重）。

物理意图：把 Plan 1 的单组 evaluate 升级为"采样→并发评估→落库→去重续跑"的跑批循环。
Plan 2 范围：budget 驱动跑 N 组新 trial，落 SQLite，返回 RunSummary。**收敛判据（Pareto/
连续K轮/覆盖度④）占位**——status 恒 "budget_exhausted"，Plan 3 才扩 "converged"。

断点续跑去重（spec §8 拷问②）：采样后先 trial_id_of + trial_exists 过滤已落库组，只把
未跑的送 Pool。write_trial 用 INSERT OR IGNORE（Plan 1）二次保险。kill/重启自动接续。

信息隔离（spec §6.2）：落库写 inner+outer（完整记录），但 RunSummary.top_inner_calmar
只用 inner（feasibility_gate 过滤后 calmar 最高）——搜索不反馈 outer，outer 留报告。
"""
from dataclasses import dataclass
import json

from discovery.sampler import sample_search
from discovery.worker import eval_batch
from discovery.store import (init_db, connect, write_trial, write_snapshot,
                             trial_id_of, trial_exists, read_trials_by_snapshot,
                             write_search_run, _now_iso)
from discovery.snapshot import SnapshotMeta, freeze
from discovery.split import HoldoutSplit
from discovery.objective import evaluate
from discovery.judging import feasibility_gate
from discovery.coverage import grid_coverage, coverage_gate
from discovery.pareto import pareto_frontier
from discovery.dsr import deflated_sharpe
from discovery.search import tpe_search, expected_improvement


def _engine_hash():
    """回测内核指纹（复用 cli._engine_hash 模式，避免循环 import：本地重声明）。

    Plan 1 cli._engine_hash 已实现（backtest.py+method_v0.py sha256[:12]）；本模块独立
    重声明同款逻辑，避免 discovery.cli → discovery.runner → discovery.cli 循环 import。
    内核（scan_symbol/risk_metrics）一动，engine_hash 变，老 trial 自然与新跑不可比。
    """
    import hashlib
    from strategies.neckline import backtest, method_v0
    h = hashlib.sha256()
    for f in (backtest.__file__, method_v0.__file__):
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


@dataclass
class RunSummary:
    """跑批汇总（Plan 3：status 扩 converged + 收敛/覆盖/EI/DSR 字段）。"""
    n_sampled: int = 0
    n_evaluated: int = 0
    n_new_trials: int = 0
    n_skipped_dup: int = 0
    n_failed: int = 0
    top_inner_calmar: float = 0.0
    top_trial_id: str = ""
    db_path: str = ""
    status: str = "budget_exhausted"   # Plan 3 扩 "converged"
    snapshot_hash: str = ""
    # Plan 3 新增
    convergence_reason: str = ""   # "coverage_met+ei_below_eps" / "budget_exhausted"
    rho: float = 0.0               # 覆盖度（判据④）
    ei: float = 0.0                # 预期提升代理（判据②）
    frontier_size: int = 0         # Pareto 前沿大小
    dsr_top: float = 0.0           # top-1 DSR（L2 统计裁决）
    run_id: str = ""               # Plan 4：本次 run 的 search_run 行 id（daemon 跨夜状态键）


def _params_key(params):
    """params dict → 可 hash 键（TPE _res_cache 用，避免双 evaluate）。"""
    return tuple(sorted((k, str(v)) for k, v in params.items()))


def _persist_trial(conn, params, snapshot_meta, split_tag, engine_hash, result, source, seed):
    """落库单 trial。返回 trial_id（新落）或 None（已存在 INSERT OR IGNORE 吞）。

    信息隔离：inner/outer 都落（完整记录供报告），但调用方排序只用 inner（spec §6.2）。
    二次保险：trial_exists 预检 + write_trial 的 INSERT OR IGNORE，防竞态下两路同 trial_id
    并发落库（预检通过到 INSERT 之间可能有其他 worker 落了同 tid）。
    """
    tid = trial_id_of(params, snapshot_meta.snapshot_hash, seed)
    if trial_exists(conn, tid):
        return None
    write_trial(conn, tid, params, snapshot_meta.snapshot_hash, engine_hash,
                split_tag, result["inner"], result["outer"], source)
    return tid


def _split_tag(split):
    """从 HoldoutSplit 生成落库 split 标签（如 'holdout_2025_2026'）。"""
    inner_y = split.inner.start.year
    outer_y = split.outer.start.year
    return f"holdout_{inner_y}_{outer_y}"


def _source_of(seed):
    """seed → source 标签（Plan 2 全用 sobol+random，source 统一 'discovery_search'）。"""
    return "discovery_search"


def run_search(snapshot_meta: SnapshotMeta, split: HoldoutSplit, budget: int,
               n_sobol: int, n_random: int, seed: int,
               db_path: str, n_proc=None, lake_start="2025-01-01",
               tpe_trials: int = 0, rho_threshold: float = 0.8,
               ei_eps: float = 1e-3) -> RunSummary:
    """跑批主函数（Plan 3：两阶段搜索 + 收敛自停 + DSR）。

    阶段一（Plan 2 Sobol 批量并发）：sample_search→去重→eval_batch→落库。
    阶段二（Plan 3 TPE 序贯，tpe_trials>0）：主进程 freeze→tpe_search（Sobol warm start，
      inner calmar 目标）→落库 TPE 新 trial（_res_cache 避免双 evaluate）。
    收敛判据（单 run）：判据④覆盖度 ρ 前置否决 + 判据②EI（TPE 后）；判据①连续K轮 + 跨 run
      EI 衰减留 Plan 4 daemon（spec §5.2）；判据③预算耗尽=budget_exhausted。
    DSR（§3.7）：top-1 trial 算 DSR 标注（L2 统计裁决，诚实报告不强选，ADR13）。
    """
    init_db(db_path)
    engine_hash = _engine_hash()
    split_tag = _split_tag(split)

    # === 阶段一：Sobol 批量并发（Plan 2 逻辑） ===
    n_sobol_eff = min(n_sobol, budget)
    n_random_eff = min(n_random, max(0, budget - n_sobol_eff))
    sampled = sample_search(n_sobol=n_sobol_eff, n_random=n_random_eff, seed=seed)
    n_sampled = len(sampled)
    with connect(db_path) as conn:
        write_snapshot(conn, snapshot_meta)
    to_eval, n_skipped = [], 0
    with connect(db_path) as conn:
        for p in sampled:
            tid = trial_id_of(p, snapshot_meta.snapshot_hash, seed)
            if trial_exists(conn, tid):
                n_skipped += 1
            else:
                to_eval.append(p)
    results = eval_batch(to_eval, lake_start=lake_start,
                         embargo_days=split.embargo_days, n_proc=n_proc) if to_eval else []
    n_new, n_failed = 0, 0
    all_evaluated = []   # 累积本 run 评估的 params（算覆盖度 ρ）
    with connect(db_path) as conn:
        for item in results:
            if item is None:
                n_failed += 1
                continue
            params, res = item
            tid = _persist_trial(conn, params, snapshot_meta, split_tag,
                                 engine_hash, res, _source_of(seed), seed)
            if tid is None:
                continue
            n_new += 1
            all_evaluated.append(params)

    # === 阶段二：TPE 序贯精化（tpe_trials>0，主进程串行 evaluate） ===
    tpe_study = None
    if tpe_trials > 0:
        universe, _ = freeze(lake_start=lake_start)   # 主进程 freeze（TPE 串行 evaluate 用）
        _res_cache = {}                                # params→res，避免落库时双 evaluate
        def _obj(p):
            res = evaluate(p, universe, split)
            _res_cache[_params_key(p)] = res
            return res["inner"].get("calmar", 0.0)
        all_params, tpe_study = tpe_search(sampled, _obj, n_trials=tpe_trials, seed=seed)
        # 落库 TPE 新 trial（sampled 之后的是 TPE 新采；缓存命中避免双跑）
        with connect(db_path) as conn:
            for p in all_params[len(sampled):]:
                res = _res_cache.get(_params_key(p)) or evaluate(p, universe, split)
                tid = _persist_trial(conn, p, snapshot_meta, split_tag,
                                     engine_hash, res, "tpe", seed)
                if tid is None:
                    continue
                n_new += 1
                all_evaluated.append(p)

    # === 收敛判据 + 覆盖度 + EI ===
    rho = grid_coverage(all_evaluated)
    ei = expected_improvement(tpe_study) if tpe_study is not None else float("inf")
    converged = False
    reason = "budget_exhausted"
    if coverage_gate(rho, rho_threshold):
        if tpe_study is not None and ei < ei_eps:
            converged = True
            reason = "coverage_met+ei_below_eps"
        # Sobol-only（无 TPE）单 run 不判 EI → budget（判据①跨 run 留 daemon）
    status = "converged" if converged else "budget_exhausted"

    # === Pareto 前沿 + DSR top-1（从 store 读所有 trial，信息隔离：只用 inner） ===
    with connect(db_path) as conn:
        trials_db = read_trials_by_snapshot(conn, snapshot_meta.snapshot_hash)
    inner_metrics = []
    for t in trials_db:
        try:
            inner_metrics.append((json.loads(t["inner_metrics"]), t))
        except (TypeError, ValueError):
            continue
    frontier_idxs = pareto_frontier([m for m, _ in inner_metrics]) if inner_metrics else []
    candidates = [(m.get("calmar", 0.0), t) for m, t in inner_metrics if feasibility_gate(m)]
    top_calmar, top_tid, dsr_top = 0.0, "", 0.0
    if candidates:
        candidates.sort(reverse=True, key=lambda x: x[0])
        top_calmar, top_t = candidates[0]
        top_tid = top_t["trial_id"]
        top_m = json.loads(top_t["inner_metrics"]) if top_t["inner_metrics"] else {}
        # DSR top-1：n_trials=本 snapshot trial 数（多重比较），n_obs=top 的交易笔数
        dsr_top = deflated_sharpe(top_m.get("sharpe", 0.0),
                                  n_trials=len(trials_db), n_obs=top_m.get("n", 30))

    # === Plan 4：落 search_run 行（daemon 跨夜状态源；cmd_run 亦受益可追溯） ===
    # 每次 run_search 写一行，同 snapshot 下多行按 started_at DESC 取最新做跨夜比对。
    # run_id = snapshot前缀 + uuid4 前8，保证同 snapshot 多夜跑批各行唯一（uuid4 每次新）。
    import uuid
    run_id = f"{snapshot_meta.snapshot_hash[:8]}_{uuid.uuid4().hex[:8]}"
    _started = _now_iso()
    with connect(db_path) as conn:
        write_search_run(conn, run_id=run_id, snapshot_hash=snapshot_meta.snapshot_hash,
                         started_at=_started, ended_at=_now_iso(), n_trials=n_new,
                         status=status, frontier_size=len(frontier_idxs),
                         k_rounds_no_expansion=0, daemon_run_count=0,
                         note=reason)

    return RunSummary(
        n_sampled=n_sampled, n_evaluated=len(to_eval), n_new_trials=n_new,
        n_skipped_dup=n_skipped, n_failed=n_failed,
        top_inner_calmar=top_calmar, top_trial_id=top_tid,
        db_path=db_path, snapshot_hash=snapshot_meta.snapshot_hash,
        status=status, convergence_reason=reason,
        rho=rho, ei=ei, frontier_size=len(frontier_idxs), dsr_top=dsr_top,
        run_id=run_id,
    )
