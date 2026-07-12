# -*- coding: utf-8 -*-
"""EMT 网关断线自动重连测试（B-8 主因）。

覆盖节点（指数退避重连核心逻辑，经 mock 验证；未实盘联调）：
  - 重连成功：前 N-1 次 connect 失败、第 N 次成功 → 提前返回，connect 调用 N 次；
  - 重连耗尽：connect 持续失败 → 重试 len(backoffs) 次后放弃，保持锁态 + 告警。

注：真实 EMT SDK（vnemttrader）联调需 Win + Python3.10 + 券商环境，本测试用 mock
覆盖重试/退避/告警的控制流；connect 的真实登录行为待实盘联调验证。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import trading.emt_gateway as emt
from trading.emt_gateway import EmtExecutionGateway


def _make_gw() -> EmtExecutionGateway:
    """构造 EMT 网关（__init__ 只读凭证，不触 SDK，无 vnemttrader 也可构造）。"""
    return EmtExecutionGateway(
        ip="1.1.1.1", port=1, user="u", password="p",
        client_id=1, sock_type=1, local_ip="127.0.0.1",
    )


@pytest.fixture(autouse=True)
def _neutralize_sleep_and_notifier(monkeypatch):
    """退避 sleep 置空（加速）+ 告警通道置空（测试不发真通知）。"""
    monkeypatch.setattr(emt.asyncio, "sleep", AsyncMock(return_value=None))
    # mock 消费 coro（close）防 "coroutine never awaited" RuntimeWarning——fire_and_forget
    # 本身在 loop 内外都能跑（新 daemon 线程 asyncio.run，已 test_notifier 验证），此 mock
    # 仅隔离真通知，必须 close coro 否则 Python 回收未 await 协程时报 RuntimeWarning。
    monkeypatch.setattr("core.notifier.fire_and_forget",
                        lambda coro=None, *a, **k: coro.close() if coro is not None else None)


def test_reconnect_succeeds_after_retries(monkeypatch):
    """前 2 次 connect 失败、第 3 次成功 → _reconnect 提前返回，共尝试 3 次。"""
    gw = _make_gw()
    attempts = []

    async def _fake_connect():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("模拟登录失败")
        # 第 3 次成功：模拟 connect 成功的副作用（清锁、置连接）
        gw._lock_down = False
        gw._connected = True

    monkeypatch.setattr(gw, "connect", _fake_connect)

    asyncio.run(gw._reconnect())

    assert len(attempts) == 3, f"应在第 3 次重连成功，实际尝试 {len(attempts)} 次"
    assert gw._lock_down is False, "重连成功应清锁"


def test_reconnect_exhausts_keeps_locked(monkeypatch):
    """connect 持续失败 → 重试 len(backoffs) 次后耗尽，保持锁态。"""
    gw = _make_gw()
    monkeypatch.setattr(emt, "_RECONNECT_BACKOFFS", (0, 0))  # 加速：2 次退避
    attempts = []

    async def _always_fail():
        attempts.append(1)
        raise ConnectionError("持续失败")

    monkeypatch.setattr(gw, "connect", _always_fail)

    asyncio.run(gw._reconnect())

    assert len(attempts) == 2, f"应重试 len(backoffs)=2 次后耗尽，实际 {len(attempts)}"
    assert gw._lock_down is True, "耗尽应保持锁态等人工介入"
