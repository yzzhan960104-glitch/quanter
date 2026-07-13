# -*- coding: utf-8 -*-
"""data_service 启动同步 sweep 测试（#6 后端启动静默更新数据）。

物理意图：验证 sweep_stale_on_startup 只对 stale/missing 数据集调 trigger_sync，
跳过 healthy/syncing/failed，且 trigger_sync 抛 KeyError（无 script）时静默不崩。
"""
from server.services import data_service


def test_sweep_stale_on_startup_triggers_stale_and_missing(monkeypatch):
    """sweep 对 stale/missing 调 trigger_sync，跳过 healthy/syncing/failed。"""
    monkeypatch.setattr(data_service, "list_datasets", lambda: [
        {"key": "daily", "status": "healthy"},
        {"key": "macro", "status": "stale"},
        {"key": "minute", "status": "missing"},
        {"key": "crypto", "status": "syncing"},
        {"key": "north_flow", "status": "failed"},
    ])
    triggered = []
    monkeypatch.setattr(data_service, "trigger_sync",
                        lambda key: triggered.append(key))
    result = data_service.sweep_stale_on_startup()
    assert sorted(result) == ["macro", "minute"]
    assert sorted(triggered) == ["macro", "minute"]


def test_sweep_stale_on_startup_skips_keyerror_silently(monkeypatch):
    """trigger_sync 抛 KeyError（无 script 配置）时静默跳过，不崩，继续后续数据集。"""
    monkeypatch.setattr(data_service, "list_datasets", lambda: [
        {"key": "stale_a", "status": "stale"},
        {"key": "stale_b", "status": "missing"},
    ])

    def _fake_trigger(key):
        if key == "stale_a":
            raise KeyError("无 script 配置")

    monkeypatch.setattr(data_service, "trigger_sync", _fake_trigger)
    result = data_service.sweep_stale_on_startup()
    assert result == ["stale_b"]   # stale_a 抛 KeyError 被跳过，stale_b 正常触发


def test_trigger_sync_concurrent_same_key_only_one_dispatches(monkeypatch, tmp_path):
    """#15：并发 trigger 同 key → 锁保证只一个派发（返"已触发"），另一个返"进行中"。

    物理意图：原哨兵 check-then-set 非原子，两并发请求都过检查 → 双 daemon 子进程互覆盖
    parquet（半截写入损坏）。加 _trigger_lock 后第二个请求必看到第一个写的哨兵而拒绝。
    barrier 让两线程同步冲入，最大化放大竞态以暴露修复前后的行为差异。
    """
    import threading as _t
    monkeypatch.setitem(data_service.DATASET_REGISTRY, "test_concurrent", {
        "script": "scripts/dummy.py", "args": [], "freshness_hours": 24,
        "source": "", "market": "", "granularity": "", "schedule": "",
    })
    monkeypatch.setattr(data_service, "SYNCING_DIR", str(tmp_path / ".syncing"))
    # 避免真起子进程：daemon 线程会调 _run_sync_subprocess，空转即可（不清哨兵，不干扰断言）
    monkeypatch.setattr(data_service, "_run_sync_subprocess", lambda key: None)

    barrier = _t.Barrier(2)
    results: list = []

    def _trigger():
        barrier.wait()   # 两线程同步冲入 trigger_sync，放大 check-then-set 竞态
        results.append(data_service.trigger_sync("test_concurrent"))

    threads = [_t.Thread(target=_trigger) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    msgs = [r["message"] for r in results]
    assert sum("请勿重复" in m for m in msgs) == 1   # 恰一个被拒（看到哨兵）
    assert sum("已触发" in m for m in msgs) == 1      # 恰一个派发（写了哨兵）
