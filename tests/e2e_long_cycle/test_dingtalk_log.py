# -*- coding: utf-8 -*-
"""V5：钉钉真推日志收集 + discovery 触发 mock。"""
from __future__ import annotations


def test_dingtalk_log_records_real_push_entries(monkeypatch):
    """enabled=True 且底层真跑协程时，应记录推送条目（design §4.5 推送可审计）。"""
    import asyncio
    from infra import notifier
    from tests.e2e_long_cycle.dingtalk_log import DingTalkLog

    async def _notify():
        return []

    def _run(coro):
        asyncio.run(coro)

    monkeypatch.setattr(notifier, "fire_and_forget", _run)
    log = DingTalkLog(enabled=True)
    with log.collect():
        notifier.fire_and_forget(_notify())
    assert len(log.records) == 1, "真推应落 1 条记录"
    assert log.records[0]["success"] is True
    assert log.records[0]["kind"].endswith("._notify"), \
        "kind 应为原始协程限定名（防包装自指）"
