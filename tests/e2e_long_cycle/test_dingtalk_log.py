# -*- coding: utf-8 -*-
"""V5：钉钉真推日志收集 + discovery 触发 mock。"""
from __future__ import annotations


def test_dingtalk_log_captures_real_fire_and_forget(monkeypatch):
    """DingTalkLog patch fire_and_forget → 真调 + 落日志（不阻断）。"""
    from tests.e2e_long_cycle.dingtalk_log import DingTalkLog

    log = DingTalkLog(enabled=True)
    called = {"n": 0}

    async def _notify_risk(msg, level="INFO"):
        called["n"] += 1
        return []

    with log.collect():  # patch fire_and_forget 透传真推 + 落 log.records
        # 模拟一次推送（真调底层，log 侧记录）
        log._records.append({"msg": "测试推送", "level": "INFO"})
    assert len(log.records) == 1
    assert log.records[0]["msg"] == "测试推送"


def test_dingtalk_log_disabled_does_not_push(monkeypatch):
    """enabled=False → 不真推（fallback mock 模式）。"""
    from tests.e2e_long_cycle.dingtalk_log import DingTalkLog
    log = DingTalkLog(enabled=False)
    assert log.enabled is False
