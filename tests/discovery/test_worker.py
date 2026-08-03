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


def test_eval_worker_is_toplevel_picklable():
    """_eval_worker 顶层定义 → 可 pickle。"""
    from discovery import worker
    blob = pickle.dumps(worker._eval_worker)
    assert pickle.loads(blob) is worker._eval_worker


def test_init_worker_not_lambda_not_nested():
    """_init_worker 不是 lambda/闭包（spawn 不可 pickle 的反例）。"""
    from discovery import worker
    fn = worker._init_worker
    assert hasattr(fn, "__qualname__")
    assert "<lambda>" not in fn.__qualname__
    assert "<locals>" not in fn.__qualname__   # 嵌套函数会有 <locals>


def test_default_n_proc_capped_at_4(monkeypatch):
    """回归（2026-08-03 资源优化）：默认进程数上限 4。

    旧实现 cpu-2（20 核机 = 18 worker）→ 12~18 个 worker 各自全量加载数据湖
    （~1.3GB/个）撑爆内存。cap 4 后 8h 夜跑预算仍覆盖 ~640 组。
    """
    from discovery import worker
    monkeypatch.delenv("DISCOVERY_N_PROC", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 20)
    assert worker._default_n_proc() == 4


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
def test_eval_batch_real_pool(champion_params):
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
