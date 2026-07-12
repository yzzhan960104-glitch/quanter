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
