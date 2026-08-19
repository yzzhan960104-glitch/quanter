# -*- coding: utf-8 -*-
"""ProcessPool worker 测试：顶层可 pickle + initializer 复用 + 单组评估返回结构。

合成 universe 快测（不真实 freeze，monkeypatch freeze 注入合成数据，避免读 455MB parquet）；
Pool 真实起子进程标 slow。
"""
import pickle
import pytest


def test_init_worker_is_toplevel_picklable():
    """_init_worker 顶层定义 → 可 pickle（Windows spawn 必需，spec §8 拷问②）。"""
    from discovery import worker
    # 模块级函数引用可 pickle（pickle by qualified name）
    blob = pickle.dumps(worker._init_worker)
    assert pickle.loads(blob) is worker._init_worker


def monkeypatch_env_scan(monkeypatch):
    """R1-1（2026-08-16）：这些用例 patch worker.evaluate（scan 口径桩）——显式钉
    DISCOVERY_OBJECTIVE=scan 让桩命中；默认口径已切 portfolio（走真组合后处理）。"""
    monkeypatch.setenv("DISCOVERY_OBJECTIVE", "scan")


def test_eval_worker_is_toplevel_picklable():
    """_eval_worker 顶层定义 → 可 pickle。"""
    from discovery import worker
    blob = pickle.dumps(worker._eval_worker)
    assert pickle.loads(blob) is worker._eval_worker


def test_default_n_proc_capped_at_16(monkeypatch):
    """P2（2026-08-13 · spec §3）：默认进程数上限 16（旧 cap 4 放开）。

    旧 cap 4 是 720s/组时代的内存务实平衡；P1 向量化 + P0-4 RSS 实测（0.57GB/worker）
    后内存不再是约束（32GB 机 ≈49 上限），cap 转向 CPU 核数（min(cpu-2, 16)）。
    内存回归防线改由 memory_cap_n_proc + _init_worker RSS 看门狗承担。
    """
    from discovery import worker
    monkeypatch.delenv("DISCOVERY_N_PROC", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 20)
    assert worker._default_n_proc() == 16


def test_default_n_proc_cpu_minus_2(monkeypatch):
    """小核数机器：cpu-2（10 核 → 8），低于 16 上限时不封顶。"""
    from discovery import worker
    monkeypatch.delenv("DISCOVERY_N_PROC", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 10)
    assert worker._default_n_proc() == 8


def test_memory_cap_n_proc_clamps(monkeypatch):
    """P2：memory_cap_n_proc 按可用内存 ÷ 每 worker RSS 估计（留储备）自动降并发。

    可用 12GB − 4GB 储备 = 8GB ÷ 2GB/worker（env 调大模拟膨胀）= 4 进程。
    """
    from discovery import worker
    monkeypatch.setenv("DISCOVERY_WORKER_RSS_GB", "2.0")
    monkeypatch.setenv("DISCOVERY_RSS_RESERVE_GB", "4.0")

    class _VM:
        available = 12 * 1024 ** 3
    monkeypatch.setattr("psutil.virtual_memory", lambda: _VM())
    assert worker.memory_cap_n_proc() == 4


def test_memory_cap_n_proc_floor_one(monkeypatch):
    """可用内存极低 → 至少 1 进程（不返 0）。psutil 不可用 → 降级返 CPU cap。"""
    from discovery import worker
    monkeypatch.setenv("DISCOVERY_WORKER_RSS_GB", "10.0")
    class _VM:
        available = 5 * 1024 ** 3
    monkeypatch.setattr("psutil.virtual_memory", lambda: _VM())
    assert worker.memory_cap_n_proc() == 1


def test_default_n_proc_env_override(monkeypatch):
    """DISCOVERY_N_PROC 环境变量显式覆盖 cap（大内存机器可放开）。"""
    from discovery import worker
    monkeypatch.setenv("DISCOVERY_N_PROC", "8")
    monkeypatch.setattr("os.cpu_count", lambda: 20)
    assert worker._default_n_proc() == 8


def test_default_n_proc_min_one(monkeypatch):
    """极端小核数/坏 env → 至少 1 进程。"""
    from discovery import worker
    monkeypatch.delenv("DISCOVERY_N_PROC", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 1)
    assert worker._default_n_proc() == 1
    monkeypatch.setenv("DISCOVERY_N_PROC", "abc")
    assert worker._default_n_proc() == 1


def test_eval_worker_uses_state(monkeypatch, champion_params, synth_sym_df):
    monkeypatch_env_scan(monkeypatch)
    """_eval_worker 在 _init_worker 设好 state 后调，返回 (params, evaluate 结果)。

    monkeypatch freeze 注入合成 universe（避免读 parquet）；_init_worker 设 _WORKER_STATE；
    _eval_worker 读 state 调 evaluate。单进程模拟（不起 Pool，验证逻辑链）。
    freeze 真实签名是 freeze(lake_start=...)，lambda 参数名须对齐（修 plan 原稿 start≠lake_start）。
    """
    from discovery import worker
    from discovery.split import HoldoutSplit, Segment
    from datetime import date

    # monkeypatch freeze 返回合成 universe + 假 meta（参数名对齐 snapshot.freeze 真实签名）
    class FakeMeta:
        snapshot_hash = "fakehash"
        universe_count = 1
    monkeypatch.setattr(worker, "freeze", lambda lake_start="2025-01-01": ({"300001.SZ": synth_sym_df}, FakeMeta()))
    monkeypatch.setattr(worker, "holdout_split", lambda embargo_days=5: HoldoutSplit(
        Segment("i", date(2025, 1, 1), date(2025, 12, 31)),
        Segment("o", date(2026, 1, 1), date(2026, 12, 31)),
        embargo_days,
    ))

    worker._init_worker("2025-01-01", 5)
    out = worker._eval_worker(champion_params)
    assert out is not None
    params_back, res = out
    assert params_back == champion_params
    assert set(res.keys()) >= {"inner", "outer", "n_total"}


def test_eval_worker_swallows_exception(monkeypatch, champion_params, synth_sym_df):
    monkeypatch_env_scan(monkeypatch)
    """_eval_worker 单组异常 → 返回 None（spec §8 单 trial 失败不影响 run）。"""
    from discovery import worker
    from discovery.split import HoldoutSplit, Segment
    from datetime import date

    class FakeMeta:
        snapshot_hash = "fakehash"
        universe_count = 1
    monkeypatch.setattr(worker, "freeze", lambda lake_start="2025-01-01": ({"300001.SZ": synth_sym_df}, FakeMeta()))
    monkeypatch.setattr(worker, "holdout_split", lambda embargo_days=5: HoldoutSplit(
        Segment("i", date(2025, 1, 1), date(2025, 12, 31)),
        Segment("o", date(2026, 1, 1), date(2026, 12, 31)), embargo_days,
    ))
    # monkeypatch evaluate 抛异常（gen.throw 在 lambda 调用时立即抛 RuntimeError）
    monkeypatch.setattr(worker, "evaluate", lambda p, u, s: (_ for _ in ()).throw(RuntimeError("boom")))

    worker._init_worker("2025-01-01", 5)
    out = worker._eval_worker(champion_params)
    assert out is None


@pytest.mark.slow
def test_eval_pool_reuses_workers(champion_params, monkeypatch):
    monkeypatch_env_scan(monkeypatch)
    """P2：EvalPool 长驻池两次 eval 同结果（worker 复用，每 worker 只 freeze 一次）。

    与 test_eval_batch_real_pool 互补：eval_batch 单发、EvalPool 长驻（TPE batch 多轮）。
    真实 spawn 2 子进程，两次 eval 各评估同一组 params，断言结果结构一致（worker 复用
    语义——第二次不重 freeze）。
    """
    from discovery.worker import EvalPool
    pool = EvalPool(n_proc=2, lake_start="2025-01-01", embargo_days=5)
    try:
        r1 = pool.eval([champion_params])
        r2 = pool.eval([champion_params])
    finally:
        pool.close()
    assert len(r1) == len(r2) == 1
    assert (r1[0] is None) == (r2[0] is None)
    if r1[0] is not None:
        _, res1 = r1[0]
        _, res2 = r2[0]
        assert res1["inner"]["n"] == res2["inner"]["n"]   # 同 params 两次评估同口径


def test_eval_batch_real_pool(champion_params, monkeypatch):
    monkeypatch_env_scan(monkeypatch)
    """集成：真实 Pool 起 2 子进程跑 2 组（含冠军 + 邻域扰动），~6min。
    验证 ProcessPool 真起作用、子进程 freeze 复用、返回非 None。"""
    from discovery.worker import eval_batch
    # 冠军 + 轻微扰动（window 80→60）作第二组
    p2 = dict(champion_params); p2["window"] = 60
    results = eval_batch([champion_params, p2], lake_start="2025-01-01",
                         embargo_days=5, n_proc=2)
    assert len(results) == 2
    # 至少冠军组应非 None（真实数据有信号）
    non_none = [r for r in results if r is not None]
    assert len(non_none) >= 1
    for params_back, res in non_none:
        assert "inner" in res and "outer" in res


def test_eval_worker_coupling6_empty_trades_returns_none(monkeypatch, champion_params, synth_sym_df):
    monkeypatch_env_scan(monkeypatch)
    """耦合6 runtime 裁剪（design 决策6）：evaluate 返回 n_total==0（挂单区间全空退化）→ None。

    spec §7.1 耦合6 buy_limit<cancel×H/ATR 依赖 runtime H/ATR（每标的每信号点不同），
    采样期无法静态判（Plan 2 收窄理由）。worker 拿 universe 后用 n_total==0 作代理：
    全 universe 无交易 = params 挂单区间全空退化。完整逐信号点裁剪需内核（ADR8 零改动），
    discovery 层只做 n_total=0 代理。
    """
    from discovery import worker
    from discovery.split import HoldoutSplit, Segment
    from datetime import date

    class FakeMeta:
        snapshot_hash = "fakehash"
        universe_count = 1
    monkeypatch.setattr(worker, "freeze", lambda lake_start="2025-01-01": ({"300001.SZ": synth_sym_df}, FakeMeta()))
    monkeypatch.setattr(worker, "holdout_split", lambda embargo_days=5: HoldoutSplit(
        Segment("i", date(2025, 1, 1), date(2025, 12, 31)),
        Segment("o", date(2026, 1, 1), date(2026, 12, 31)), embargo_days))
    # monkeypatch evaluate 返回 n_total=0（挂单区间全空退化）
    monkeypatch.setattr(worker, "evaluate", lambda p, u, s: {"inner": {"ann": 0}, "outer": {"ann": 0}, "n_total": 0})

    worker._init_worker("2025-01-01", 5)
    out = worker._eval_worker(champion_params)
    assert out is None   # 耦合6 runtime 裁剪
