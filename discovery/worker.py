# -*- coding: utf-8 -*-
"""L2 ProcessPool worker（spec §4.3 / §8 拷问②，Windows spawn 兼容）。

物理意图：Plan 1 单组 evaluate ~3min，串行跑 N 组要 N×3min。ProcessPool 并发把 N 组
分到 (核数-2) 子进程，吞吐 ×(核数-2)（spec §3.6 算力账）。每子进程一次 freeze 加载
universe（~5s，455MB parquet），后续多组复用——避免每 trial 重读 parquet（ initializer
模式：universe 进子进程模块全局，不随每 params pickle，否则 455MB 每次 map 序列化爆掉）。

Windows spawn 兼容（spec §8 拷问② + Global Constraints，四条铁律）：
1. _init_worker / _eval_worker 必须**顶层定义**——spawn 用 pickle 序列化函数引用，嵌套/
   lambda/闭包不可 pickle 会 raise AttributeError（test_worker.py 三个 pickle/qualname 测试守此）。
2. 子进程会重新 import 本模块——顶层不能有副作用（不能 import 时 freeze，_WORKER_STATE
   初始 ready=False 占位，待 initializer 填充）。
3. universe 通过 initializer 注入子进程模块全局 _WORKER_STATE，**不随每 params pickle**
   （否则 455MB parquet 每次 map 都序列化，爆掉）。
4. _eval_worker 捕获单组异常返回 None——主进程 filter null（spec §8 单 trial 失败不影响 run）。
"""
import multiprocessing as mp
import os

# 顶层 import（子进程重新 import 时执行，无副作用）。
# 用 from-import 让 monkeypatch 可 patch worker.freeze/holdout_split/evaluate（测试注入合成数据）。
from discovery.snapshot import freeze
from discovery.split import holdout_split
from discovery.objective import evaluate

# 子进程模块全局：initializer 一次 freeze 后填充，_eval_worker 读取复用。
# 主进程也 import 本模块（读 _WORKER_STATE.ready=False 占位），但主进程不调 _eval_worker。
_WORKER_STATE = {"universe": None, "split": None, "ready": False}


def _init_worker(lake_start="2025-01-01", embargo_days=5):
    """Pool initializer：子进程启动时一次 freeze 加载 universe + split 到 _WORKER_STATE。

    顶层定义（可 pickle，spawn 必需）。lake_start/embargo_days 是简单类型（str/int），
    可 pickle 跨进程传 initargs。后续 _eval_worker 复用此 universe，不重读 parquet。
    """
    universe, _meta = freeze(lake_start=lake_start)
    split = holdout_split(embargo_days=embargo_days)
    _WORKER_STATE["universe"] = universe
    _WORKER_STATE["split"] = split
    _WORKER_STATE["ready"] = True


def _eval_worker(params):
    """Pool.map 调用：评估单组 params，返回 (params, result_dict) 或 None（异常/退化）。

    顶层定义（可 pickle）。读 _WORKER_STATE（initializer 设的 universe/split），调
    objective.evaluate。两类返回 None：
    1. 异常（spec §8 拷问②：worker 崩溃 → 单 trial 标 failed，run 继续）。
    2. 耦合6 runtime 裁剪（design 决策6，spec §7.1）：n_total==0 = 全 universe 挂单区间
       全空（params 退化）→ None。完整逐信号点裁剪需内核（ADR8 零改动），discovery 层
       只做 n_total=0 代理。
    """
    if not _WORKER_STATE["ready"]:
        return None
    try:
        res = evaluate(params, _WORKER_STATE["universe"], _WORKER_STATE["split"])
        # 耦合6 runtime 裁剪：n_total==0 = 挂单区间全空退化（spec §7.1 耦合6 代理）
        if res.get("n_total", 0) == 0:
            return None
        return (params, res)
    except Exception:
        return None


def _default_n_proc():
    """默认进程数：核数-2，但**上限 4**（2026-08-03 资源优化）。

    物理意图：12~18 个 worker 各自 freeze() 全量加载数据湖（每 worker ~1.3GB）是
    内存峰值来源——曾把 32GB 机器压到 2.7GB 空闲，触发 MemoryError/WinError 5
    （discovery_cron.log 实证）。颈线法单组 ~3min，4 并发 × 8h 夜跑预算 ≈ 640 组，
    远高于单夜实际样本量；cap 4 是吞吐/内存的务实平衡。
    环境变量 DISCOVERY_N_PROC 可显式覆盖（大内存机器可调回 8）。
    """
    env = os.environ.get("DISCOVERY_N_PROC")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return min(4, max(1, (os.cpu_count() or 4) - 2))


def eval_batch(params_list, lake_start="2025-01-01", embargo_days=5, n_proc=None):
    """便捷封装：主进程建 Pool + initializer + map _eval_worker + 收集结果。

    返回 list，每项为 (params, result_dict) 或 None（异常组）。调用方按需 filter null。
    n_proc=None → 默认核数-2；再 clamp 到不超过任务数（开 8 进程跑 2 任务无意义）。
    空输入返回 []（不起 Pool，省 spawn 开销）。
    """
    if not params_list:
        return []
    if n_proc is None:
        n_proc = _default_n_proc()
    n_proc = min(n_proc, len(params_list))   # 不超过任务数
    # Windows spawn：显式 get_context("spawn")（Win 默认即 spawn，显式更清晰可移植；Linux 上
    # 默认 fork 但 spawn 更安全——避免 fork 继承父进程内存/锁状态的坑）。
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=n_proc, initializer=_init_worker,
                  initargs=(lake_start, embargo_days)) as pool:
        results = pool.map(_eval_worker, params_list)
    return results
