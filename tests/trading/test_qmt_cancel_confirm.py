# -*- coding: utf-8 -*-
"""M2 撤单确认闭环单测：cancel 后轮询到终态才返 True。

物理背景（[[qmt-live-smoke-findings]]）：
    QMT cancel_order 调用后主推回报延迟 1-2s，原代码直接计数不确认终态，
    导致撤单状态悬空（本地以为撤了、柜台其实没撤）。_confirm_cancelled
    轮询 query_orders 直到该 oid 到 CANCELLED/FILLED/REJECTED/PARTIAL_CANCELLED
    终态或超时，让上层据 True/False 决定是否告警/重试。
"""
import pytest
from unittest.mock import AsyncMock

from trading.types.order_state import OrderState


def _gw_with_orders(monkeypatch, orders_factory):
    """造一个 QmtExecutionGateway，query_orders 被 AsyncMock 替换为 orders_factory()。

    构造方式对齐 tests/trading/test_qmt_gateway.py 的项目惯例：
        QmtExecutionGateway 必须有 QMT_USERDATA_PATH / QMT_ACCOUNT_ID 才能实例化，
        故用 monkeypatch.setenv 注入假值（不污染其他测试）。

    签名注意：_confirm_cancelled 内部以 query_orders(cancelable_only=False) 调用，
    故 orders_factory 必须吸收该 kwarg（brief 原 lambda 无参 → TypeError 被
    实现 except 吞掉致 CANCELLED/FILLED 用例假失败）。
    """
    from broker.qmt import QmtExecutionGateway
    monkeypatch.setenv("QMT_USERDATA_PATH", "D:\\fake")
    monkeypatch.setenv("QMT_ACCOUNT_ID", "1000000365")
    gw = QmtExecutionGateway()

    def _factory(*args, **kwargs):
        return orders_factory()

    gw.query_orders = AsyncMock(side_effect=_factory)
    return gw


@pytest.mark.asyncio
async def test_confirm_cancelled_returns_true_on_cancelled(monkeypatch):
    """撤单后查到 CANCELLED 终态 → 返 True。"""
    # query_orders 契约：返 list[dict]；故每次返回包成单元素 list
    # （brief 原 seq.pop(0) 返裸 dict 迭代出 keys，与 query_orders 契约不符）
    seq = [{"order_id": 111, "state": OrderState.SUBMITTED},
           {"order_id": 111, "state": OrderState.CANCELLED}]
    gw = _gw_with_orders(monkeypatch, lambda: [seq.pop(0)] if seq else [])
    ok = await gw._confirm_cancelled("111", timeout=2.0, interval=0.05)
    assert ok is True


@pytest.mark.asyncio
async def test_confirm_cancelled_timeout_returns_false(monkeypatch):
    """一直非终态 → 超时返 False（绝不假装成功）。"""
    gw = _gw_with_orders(monkeypatch, lambda: [{"order_id": 111, "state": OrderState.SUBMITTED}])
    ok = await gw._confirm_cancelled("111", timeout=0.2, interval=0.05)
    assert ok is False


@pytest.mark.asyncio
async def test_confirm_cancelled_filled_is_terminal(monkeypatch):
    """撤单时已 FILLED（撤单失败但状态明确）→ 返 True（终态确认）。"""
    gw = _gw_with_orders(monkeypatch, lambda: [{"order_id": 111, "state": OrderState.FILLED}])
    ok = await gw._confirm_cancelled("111", timeout=1.0, interval=0.05)
    assert ok is True


@pytest.mark.asyncio
async def test_confirm_cancelled_lockdown_returns_false(monkeypatch):
    """lock_down 时 query_orders 返[]（降级）→ 超时返 False。"""
    gw = _gw_with_orders(monkeypatch, lambda: [])
    ok = await gw._confirm_cancelled("111", timeout=0.2, interval=0.05)
    assert ok is False
