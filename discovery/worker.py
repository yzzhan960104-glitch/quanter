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
import sys

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

    P2 RSS 看门狗（spec §3）：freeze 后量本 worker RSS，超阈值 fail-loud 退出（stderr
    CRITICAL + os._exit(3)）——2026-08-03 MemoryError 的教训是"静默 OOM"，数据湖膨胀/
    列裁剪失效会瞬间把单 worker 推到 1.3GB+ 时代，看门狗让异常**响**出来而非压垮机器。
    看门狗自身异常吞掉（测量失败降级不阻断跑批）。
    """
    universe, _meta = freeze(lake_start=lake_start)
    split = holdout_split(embargo_days=embargo_days)
    _WORKER_STATE["universe"] = universe
    _WORKER_STATE["split"] = split
    _WORKER_STATE["ready"] = True
    try:
        import psutil   # requirements.txt 已有（P0-4 diag 引入）
        rss_gb = psutil.Process(os.getpid()).memory_info().rss / (1024 ** 3)
        if rss_gb > _WORKER_RSS_MAX_GB:
            print(f"[discovery-worker][CRITICAL] freeze 后 RSS {rss_gb:.2f}GB > 看门狗阈值 "
                  f"{_WORKER_RSS_MAX_GB}GB——疑似数据湖膨胀/列裁剪失效，worker 主动退出",
                  file=sys.stderr, flush=True)
            os._exit(3)
    except SystemExit:
        raise
    except Exception:
        pass   # 看门狗自身异常（psutil 缺失等）降级：不阻断跑批


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


# P2（2026-08-13 · spec §3）吞吐常量：
# - _N_PROC_CPU_CAP：CPU 约束上限（20 核机器 = 16）。旧 cap 4 是 720s/组时代的内存务实平衡，
#   P1 向量化 + P0-4 RSS 实测（0.57GB/worker）后内存不再是约束（49 上限），瓶颈转向 CPU。
# - _WORKER_RSS_GB_EST：每 worker RSS 估计（P0-4 实测 0.57GB，取 1.0 留余量）——memory_cap_n_proc
#   按「可用内存 ÷ 该估计」自动降并发，防数据膨胀/列裁剪失效回归。
# - _RSS_RESERVE_GB：留给 OS/主进程的储备（2026-08-03 压到 2.7GB 空闲的教训）。
# - _WORKER_RSS_MAX_GB：单 worker fail-loud 阈值（正常 0.57GB，超 6GB 必是异常回归）。
_N_PROC_CPU_CAP = 16
_WORKER_RSS_MAX_GB = float(os.environ.get("DISCOVERY_WORKER_RSS_MAX_GB", "6.0"))


def _default_n_proc():
    """默认进程数：min(核数-2, 16)——P2 从 cap 4 放开（spec §3）。

    物理依据（P0-4 实测）：per-worker RSS 0.57GB（lake_start=2025 列裁剪后），32GB 机
    内存上限 ≈49 worker——内存不再是约束，cap 转向 CPU 核数。2026-08-03 MemoryError
    的复发防线改由 memory_cap_n_proc（可用内存公式自动降并发）+ _init_worker RSS
    看门狗（fail-loud）承担，不再用硬 cap 4 一刀切。
    环境变量 DISCOVERY_N_PROC 可显式覆盖（仍优先）。
    """
    env = os.environ.get("DISCOVERY_N_PROC")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return min(_N_PROC_CPU_CAP, max(1, (os.cpu_count() or 4) - 2))


def memory_cap_n_proc():
    """内存上限（P2 自动降并发）：可用内存 ÷ 每 worker RSS 估计（留储备）。

    与 _default_n_proc 的 CPU 上限取 min 得最终并发。psutil 不可用（异常）→ 降级返
    CPU cap（只留 CPU 约束，看门狗仍兜底）。估计值 _WORKER_RSS_GB_EST 来自 P0-4 实测
    0.57GB/worker 取 1.0 余量；数据湖膨胀时公式自然收紧，无需人工干预。
    """
    # env 在函数内读（非模块级常量）：测试/运维可运行时调参不重启进程
    rss_est_gb = float(os.environ.get("DISCOVERY_WORKER_RSS_GB", "1.0"))
    reserve_gb = float(os.environ.get("DISCOVERY_RSS_RESERVE_GB", "4.0"))
    try:
        import psutil
        avail_gb = psutil.virtual_memory().available / (1024 ** 3)
    except Exception:
        return _N_PROC_CPU_CAP
    return max(1, int((avail_gb - reserve_gb) / rss_est_gb))


def eval_batch(params_list, lake_start="2025-01-01", embargo_days=5, n_proc=None):
    """便捷封装：主进程建 Pool + initializer + map _eval_worker + 收集结果。

    返回 list，每项为 (params, result_dict) 或 None（异常组）。调用方按需 filter null。
    n_proc=None → 默认核数-2；再 clamp 到不超过任务数（开 8 进程跑 2 任务无意义）与
    内存上限（P2 memory_cap_n_proc）。空输入返回 []（不起 Pool，省 spawn 开销）。
    """
    if not params_list:
        return []
    if n_proc is None:
        n_proc = _default_n_proc()
    n_proc = min(n_proc, memory_cap_n_proc(), len(params_list))   # 任务数 + 内存上限双 clamp
    # Windows spawn：显式 get_context("spawn")（Win 默认即 spawn，显式更清晰可移植；Linux 上
    # 默认 fork 但 spawn 更安全——避免 fork 继承父进程内存/锁状态的坑）。
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=n_proc, initializer=_init_worker,
                  initargs=(lake_start, embargo_days)) as pool:
        results = pool.map(_eval_worker, params_list)
    return results


class EvalPool:
    """长驻评估进程池（P2 · spec §3）：TPE batch 多轮 ask/tell 复用同一批 worker。

    物理意图：eval_batch 每次调用都新建 Pool → 每 worker 重 spawn + 重 freeze（~20-30s
    数据湖加载）。TPE batch 需要多轮「ask K → 评估 → tell」，若每轮调 eval_batch 会
    反复付 freeze 成本。EvalPool 建一次、多轮 eval、显式 close——每 worker 全程只
    freeze 一次。构造时同样过 memory_cap_n_proc（自动降并发）。单发调用方继续用
    eval_batch（语义不变）。
    """

    def __init__(self, n_proc=None, lake_start="2025-01-01", embargo_days=5):
        if n_proc is None:
            n_proc = _default_n_proc()
        n_proc = min(n_proc, memory_cap_n_proc())
        ctx = mp.get_context("spawn")
        self._pool = ctx.Pool(processes=n_proc, initializer=_init_worker,
                              initargs=(lake_start, embargo_days))

    def eval(self, params_list):
        """评估一批 params → list[(params, result)|None]（与 eval_batch 同语义）。"""
        if not params_list:
            return []
        return self._pool.map(_eval_worker, params_list)

    def close(self):
        """关闭池（close + join，释放 worker）。with 语义不提供——显式 close 更直白。"""
        self._pool.close()
        self._pool.join()
