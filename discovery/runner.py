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

from discovery.sampler import sample_search
from discovery.worker import eval_batch
from discovery.store import (init_db, connect, write_trial, write_snapshot,
                             trial_id_of, trial_exists)
from discovery.snapshot import SnapshotMeta
from discovery.split import HoldoutSplit
from discovery.judging import feasibility_gate


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
    """跑批汇总（Plan 2：无收敛判据，status 恒 budget_exhausted；Plan 3 扩 converged）。"""
    n_sampled: int = 0          # 采样总数（filter_feasible 后）
    n_evaluated: int = 0        # 实际送 Pool 评估数（采样 - 已存去重）
    n_new_trials: int = 0       # 新落库 trial 数
    n_skipped_dup: int = 0      # 跳过的已存 trial（断点续跑去重）
    n_failed: int = 0           # 评估失败组数（eval_batch 返回 None）
    top_inner_calmar: float = 0.0   # 可行域内最高 inner calmar（信息隔离：不用 outer）
    top_trial_id: str = ""      # top_inner_calmar 对应的 trial_id
    db_path: str = ""
    status: str = "budget_exhausted"   # Plan 3 扩 "converged"
    snapshot_hash: str = ""


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
               db_path: str, n_proc=None, lake_start="2025-01-01") -> "RunSummary":
    """跑批主函数：采样→去重→并发评估→落库→汇总。

    流程（spec §5.1）：
    1. sample_search 产 n_sobol+n_random 合法 params（约束裁剪后），budget 截断目标量
    2. write_snapshot 落 snapshot（upsert，每 run 刷新）
    3. trial_id_of+trial_exists 过滤已落库组（断点续跑去重）
    4. eval_batch 并发评估未跑组（Pool, initializer 复用 universe）
    5. _persist_trial 落库（INSERT OR IGNORE 二次保险）
    6. RunSummary 汇总（top_inner_calmar 用 inner，信息隔离）

    snapshot_meta: SnapshotMeta（freeze 已算好，避免每 run 重 freeze）。
    split: HoldoutSplit。budget: 采样目标上限（n_sobol+n_random 受其截断）。
    返回 RunSummary（status 恒 budget_exhausted，Plan 3 才扩 converged）。
    """
    init_db(db_path)
    engine_hash = _engine_hash()
    split_tag = _split_tag(split)

    # 1. 采样（约束裁剪后合法流）；budget 截断目标量（先 sobol 后 random）
    n_sobol_eff = min(n_sobol, budget)
    n_random_eff = min(n_random, max(0, budget - n_sobol_eff))
    sampled = sample_search(n_sobol=n_sobol_eff, n_random=n_random_eff, seed=seed)
    n_sampled = len(sampled)

    # 2. 落 snapshot（每 run 刷新，upsert）
    with connect(db_path) as conn:
        write_snapshot(conn, snapshot_meta)

    # 3. 去重：过滤已落库组（断点续跑）
    to_eval = []
    n_skipped = 0
    with connect(db_path) as conn:
        for p in sampled:
            tid = trial_id_of(p, snapshot_meta.snapshot_hash, seed)
            if trial_exists(conn, tid):
                n_skipped += 1
            else:
                to_eval.append(p)

    # 4. 并发评估（空则跳过，避免 Pool 空转）
    results = []
    if to_eval:
        results = eval_batch(to_eval, lake_start=lake_start,
                             embargo_days=split.embargo_days, n_proc=n_proc)

    # 5. 落库 + 汇总
    n_new = 0
    n_failed = 0
    candidates = []   # (calmar, tid) for top
    with connect(db_path) as conn:
        for item in results:
            if item is None:
                n_failed += 1
                continue
            params, res = item
            tid = _persist_trial(conn, params, snapshot_meta, split_tag,
                                 engine_hash, res, _source_of(seed), seed)
            if tid is None:
                continue   # 竞态下被 IGNORE 吞（罕见）
            n_new += 1
            inner = res["inner"]
            if feasibility_gate(inner):
                candidates.append((inner["calmar"], tid))

    # top_inner_calmar（信息隔离：只用 inner）
    top_calmar = 0.0
    top_tid = ""
    if candidates:
        candidates.sort(reverse=True)
        top_calmar, top_tid = candidates[0]

    return RunSummary(
        n_sampled=n_sampled,
        n_evaluated=len(to_eval),
        n_new_trials=n_new,
        n_skipped_dup=n_skipped,
        n_failed=n_failed,
        top_inner_calmar=top_calmar,
        top_trial_id=top_tid,
        db_path=db_path,
        snapshot_hash=snapshot_meta.snapshot_hash,
    )
