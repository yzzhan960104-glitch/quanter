# -*- coding: utf-8 -*-
"""M2 接入（Task 3）：cancel_all_open_orders 撤单后调 _confirm_cancelled，统计 unconfirmed。

物理背景（spec M2 · [[qmt-live-smoke-findings]]）：
    QMT cancel_order 调用后主推回报延迟 1-2s，原 cancel_all_open_orders 撤单后只数
    「发起撤单笔数」即返，不确认柜台是否真撤成 → 状态悬空（本地以为撤了、柜台没撤）。
    本测试验证 breaker.py 接入 T2 产出的 ``gw._confirm_cancelled``：
      - 撤单成功但确认超时 → 计入 unconfirmed（不假装成功）
      - 网关无该方法（Mock/老网关）→ 鸭子类型跳过，行为向后兼容（unconfirmed=0）
      - 返回值同时含 cancelled 与 unconfirmed 两个口径

Why 数据源用 gw._orders dict（非 query_orders）：
    cancel_all_open_orders 真实实现遍历 ``getattr(gw, "_orders", {})``（与
    QmtExecutionGateway 同口径），故测试也用 _orders 构造可撤单集合，对齐真实结构。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from trading.io import breaker
from trading.types import OrderState


@pytest.mark.asyncio
async def test_cancel_all_counts_unconfirmed_on_timeout():
    """撤单成功但 _confirm_cancelled 超时 → 计入 unconfirmed（不假装成功）。

    场景：pre_open 撤 2 笔未终态单，cancel_order 都成功发起，但柜台主推延迟
    致 _confirm_cancelled 超时返 False → unconfirmed=2（需告警人工复核）。
    """
    gw = MagicMock()
    gw.cancel_order = AsyncMock(return_value=None)
    gw._confirm_cancelled = AsyncMock(return_value=False)  # 超时未确认
    # _orders：2 笔未终态可撤单（state=SUBMITTED 非终态集）
    gw._orders = {
        "1": {"state": OrderState.SUBMITTED},
        "2": {"state": OrderState.SUBMITTED},
    }
    res = await breaker.cancel_all_open_orders(gw)
    # cancelled=2（成功发起撤单），unconfirmed=2（但都没确认到终态）
    assert res["cancelled"] == 2
    assert res["unconfirmed"] == 2


@pytest.mark.asyncio
async def test_cancel_all_confirmed_zero_when_all_terminal():
    """撤单且全部确认到终态 → unconfirmed=0（正常路径）。

    场景：2 笔未终态单，cancel_order 成功 + _confirm_cancelled 都返 True。
    """
    gw = MagicMock()
    gw.cancel_order = AsyncMock(return_value=None)
    gw._confirm_cancelled = AsyncMock(return_value=True)  # 全部确认到终态
    gw._orders = {
        "1": {"state": OrderState.SUBMITTED},
        "2": {"state": OrderState.SUBMITTED},
    }
    res = await breaker.cancel_all_open_orders(gw)
    assert res["cancelled"] == 2
    assert res["unconfirmed"] == 0


@pytest.mark.asyncio
async def test_cancel_all_backward_compat_without_confirm_method():
    """网关无 _confirm_cancelled 方法（Mock/老网关）→ 鸭子类型跳过，向后兼容。

    Why 鸭子类型 getattr：
        breaker.py 是 io 层副作用壳，不该硬依赖 QmtExecutionGateway 具体类；
        dry_run/测试 Mock 可能不挂该方法。getattr 判存让无方法时退化为「不确认」，
        unconfirmed 恒 0，cancel 计数照常，既有调用方零回归。
    """
    gw = MagicMock()
    gw.cancel_order = AsyncMock(return_value=None)
    # 故意不设 _confirm_cancelled（MagicMock spec 未声明 → getattr 返真 Mock 对象会破坏
    # 判空逻辑，故用 spec 限定属性集，使 getattr(gw, "_confirm_cancelled", None) 返 None）
    gw = MagicMock(spec=["cancel_order"])
    gw.cancel_order = AsyncMock(return_value=None)
    gw._orders = {"1": {"state": OrderState.SUBMITTED}}
    res = await breaker.cancel_all_open_orders(gw)
    # 无确认方法：cancelled 照数，unconfirmed=0（无法确认即不计未确认，保持旧行为）
    assert res["cancelled"] == 1
    assert res["unconfirmed"] == 0
