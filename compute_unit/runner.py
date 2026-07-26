# -*- coding: utf-8 -*-
"""跑批核心:verify_and_freeze → spawn Pool 并行 evaluate → 拼 Result。

物理意图(spec §9.2 拷问②):一组 params × 全 universe ~720s 串行;一批 N 组用 mp.spawn
Pool 分到 (核数-2) 子进程并发,吞吐 ×(核数-2)。完全沿用 discovery.worker 的 spawn 四铁律
(C7,Win/Linux/Mac 跨平台一致):
1. _init_worker/_eval_worker/_eval_one 顶层定义(spawn pickle 函数引用,嵌套/lambda 不可 pickle)
2. 子进程重新 import 本模块——顶层无副作用(_WORKER_STATE 初始 ready=False 占位)
3. universe 经 initializer 注入子进程模块全局,不随每 params pickle(否则 455MB 每次 map 爆掉)
4. _eval_one 捕获单组异常 → failed;n_total==0 退化 → degenerate(对应 _eval_worker 返 None 语义)

可测试拆分:_eval_one 是纯函数(注入 universe+split,同进程 mock evaluate 生效);_eval_worker
是 spawn 包装(读 _WORKER_STATE);_eval_batch 注入 universe(单测不依赖 freeze/parquet)。
"""
from __future__ import annotations

import multiprocessing as mp
import os
from datetime import date

from compute_unit.protocol import Task, Result, TrialResult, SplitSpec


# 子进程模块全局:initializer 一次注入后填充(主进程也 import 本模块但读 ready=False 占位,
# 主进程不调 _eval_worker)。
_WORKER_STATE = {"universe": None, "split": None, "ready": False}


def _split_to_discovery(split_spec: SplitSpec):
    """protocol.SplitSpec → discovery.split.HoldoutSplit(evaluate 需要的类型)。

    date 字段原样透传(SplitSpec.start/end 已是 datetime.date)。
    """
    from discovery.split import HoldoutSplit, Segment
    return HoldoutSplit(
        inner=Segment(split_spec.inner.name, split_spec.inner.start, split_spec.inner.end),
        outer=Segment(split_spec.outer.name, split_spec.outer.start, split_spec.outer.end),
        embargo_days=split_spec.embargo_days,
    )


def _eval_one(trial, universe, split) -> TrialResult:
    """评估单 trial(纯函数,无全局状态)。_eval_worker 是它的 spawn 包装。

    两类降级(C7):
    - 异常 → status=failed(单 trial 崩溃不阻断批,对应 discovery._eval_worker 返 None)
    - n_total==0 → status=degenerate(params 退化,全 universe 挂单区间空)
    """
    from discovery.objective import evaluate
    try:
        res = evaluate(trial.params, universe, split)
    except Exception as e:
        return TrialResult(trial_id=trial.trial_id, status="failed", error=repr(e)[:200])
    if res.get("n_total", 0) == 0:
        return TrialResult(trial_id=trial.trial_id, status="degenerate", n_total=0)
    return TrialResult(trial_id=trial.trial_id, status="ok",
                       inner=res["inner"], outer=res["outer"], n_total=res["n_total"])


def _init_worker(universe, split_spec: SplitSpec):
    """Pool initializer:子进程启动时一次注入 universe + split 到 _WORKER_STATE。

    顶层定义(spawn 可 pickle)。universe 是 dict(主进程 freeze 好传入),不重读 parquet。
    split 经 _split_to_discovery 转 HoldoutSplit(evaluate 需要的类型)。
    """
    _WORKER_STATE["universe"] = universe
    _WORKER_STATE["split"] = _split_to_discovery(split_spec)
    _WORKER_STATE["ready"] = True


def _eval_worker(trial) -> TrialResult:
    """Pool.map 调用:读 _WORKER_STATE → _eval_one。顶层定义(spawn 可 pickle)。"""
    if not _WORKER_STATE["ready"]:
        return TrialResult(trial_id=trial.trial_id, status="failed", error="worker 未就绪")
    return _eval_one(trial, _WORKER_STATE["universe"], _WORKER_STATE["split"])


def _default_n_proc() -> int:
    """默认进程数 = 核数 - 2(留 2 核给系统/主进程,与 discovery.worker._default_n_proc 同)。"""
    return max(1, (os.cpu_count() or 4) - 2)


def _eval_batch(task: Task, universe: dict, split_spec: SplitSpec,
                n_proc: int | None = None) -> list:
    """纯计算:spawn Pool 并行评估 task.trials → list[TrialResult]。

    可测试入口(注入 universe,不依赖 freeze/parquet)。生产入口 run() 调它。
    n_proc=None → 核数-2;clamp 到不超过任务数(开 8 进程跑 2 任务无意义)。
    空 trials 返 [](不起 Pool,省 spawn 开销)。
    """
    if not task.trials:
        return []
    if n_proc is None:
        n_proc = _default_n_proc()
    n_proc = min(n_proc, len(task.trials))
    ctx = mp.get_context("spawn")   # 跨平台一致(Win 默认 spawn,Linux/Mac 显式更安全,不踩 fork 坑)
    with ctx.Pool(processes=n_proc, initializer=_init_worker,
                  initargs=(universe, split_spec)) as pool:
        results = pool.map(_eval_worker, task.trials)
    return results


def run(task: Task, n_proc: int | None = None) -> Result:
    """生产入口:verify_and_freeze(校验+freeze 复用)→ _eval_batch → 拼 Result。

    verify_and_freeze 漂移抛 EnvDriftError(__main__ 捕获返退出码 3)。返回的 universe 直接
    喂 _eval_batch(不重复 freeze,省 ~5s)。
    """
    from compute_unit.env_check import verify_and_freeze
    from datetime import datetime
    universe, _meta = verify_and_freeze(task)
    trial_results = _eval_batch(task, universe, task.split, n_proc=n_proc)
    return Result(
        task_id=task.task_id,
        git_commit=task.git_commit,
        parquet_sha256=task.parquet_sha256,
        ran_at=datetime.utcnow().isoformat(),
        results=trial_results,
    )
