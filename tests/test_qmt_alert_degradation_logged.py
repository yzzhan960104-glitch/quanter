# -*- coding: utf-8 -*-
"""G7 告警可观测测试（broker/qmt.py 告警通道 5 处 except:pass 软降级）。

物理意图（task-G7 Part A · 消「监控监控器」盲区）：
    broker/qmt.py 的 5 处告警 fire_and_forget 外层 ``except Exception: pass``（账号状态
    告警 / 断线告警 / 重连成功·失败·耗尽告警）是「告警系统自己的兜底」——告警发送失败
    时吞掉异常不阻断主路径（重连/交易），正确。但原零日志 = 「监控监控器」盲区典型：
    告警通道（钉钉网络/get_default）死掉时无人知晓，运维以为告警在推实际全失效。
    本测试断言：告警通道异常时 caplog 有 debug 记录（含 exc_info），且**控制流不变**
    （不抛异常阻断主路径）。

覆盖：_alert_account_status（模块函数）+ _on_disconnect_fatal（实例方法，loop=None
隔离 create_task 只测告警 except）。_reconnect 内 3 处告警 except 同范式（重连成功/
失败/耗尽），源码可见同 ``logger.debug("告警通道软降级：...", exc_info=True)`` 模式。

TDD RED→GREEN。全中文注释。
"""
from __future__ import annotations

import asyncio
import logging


def test_alert_account_status_logs_on_failure(monkeypatch, caplog):
    """_alert_account_status: 告警通道异常 → 不抛（控制流不变）+ caplog 有 debug。

    物理意图：账号状态告警的 fire_and_forget 通道失败（钉钉网络故障/get_default 异常）
    不应阻断主路径，但原 except:pass 零日志 → 告警系统静默死掉。加 debug 让失效可观测。
    """
    import broker.qmt as qmt
    from infra import notifier

    def boom():
        raise RuntimeError("notifier 注入失败")

    monkeypatch.setattr(notifier.NotificationManager, "get_default", boom)

    class FakeGw:
        _account_id = "TEST_ACC"

    with caplog.at_level(logging.DEBUG, logger="broker.qmt"):
        # 控制流不变：不抛异常（告警通道软降级，主路径继续）
        qmt._alert_account_status(FakeGw(), 1, "WARN")

    # 可观测：caplog 有 debug 含「告警通道软降级」
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("告警通道软降级" in m and "TEST_ACC" in m for m in msgs), \
        f"期望账号状态告警软降级 debug 日志，实得：{msgs}"


def test_on_disconnect_fatal_logs_alert_failure(monkeypatch, caplog):
    """_on_disconnect_fatal: 断线告警通道异常 → 不抛（控制流不变）+ caplog 有 debug。

    物理意图：断线告警是运维介入的唯一信号源，告警通道失效 = 运维完全失明。loop=None
    隔离 create_task 分支（不真跑重连），只测告警 except 的可观测性。
    """
    import broker.qmt as qmt
    from infra import notifier

    def boom():
        raise RuntimeError("notifier 注入失败")

    monkeypatch.setattr(notifier.NotificationManager, "get_default", boom)

    # 构造未连接 gw（__new__ 跳过 __init__ 的重连接逻辑），loop=None 隔离 create_task
    gw = qmt.QmtExecutionGateway.__new__(qmt.QmtExecutionGateway)
    gw._account_id = "TEST_ACC"
    gw._loop = None  # 跳过 create_task(self._reconnect())，只测告警 except

    with caplog.at_level(logging.DEBUG, logger="broker.qmt"):
        # 控制流不变：不抛异常（断线告警软降级，重连主路径不受影响）
        gw._on_disconnect_fatal()

    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("告警通道软降级" in m and "断线" in m for m in msgs), \
        f"期望断线告警软降级 debug 日志，实得：{msgs}"
