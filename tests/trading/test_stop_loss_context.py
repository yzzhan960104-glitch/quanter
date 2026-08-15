# -*- coding: utf-8 -*-
"""StopLossContext 值对象契约 + stop_loss_monitor 单参收口语义（tech-debt M2 · 2026-08-15）。

物理意图：_stoploss → stop_loss_monitor 原散传 stop_prices/monitor_ctx/pending_ctx 三个
同形 dict，本组测试钉死 M2 收口后的四条契约：

1. frozen 值对象：三 map 字段可读不可写（防下游偷偷塞第四个 map 逃逸收口）；
2. 缺省全 None（向后兼容裸调 monitor() 的旧契约）；
3. monitor 签名单参收 StopLossContext：空 dict → None 归一后走「均空 no-op」既定分支
   （延续 engine 旧调用点 ``stop_prices or None`` 契约，行为等价红线）；
4. context=None（不传）与空 context 行为等价（test_engine.py:578 裸调路径不破）。

测试边界：不真起 APScheduler、无真行情真单——calendar/qmd/get_gateway 全 patch，
monitor 走到「无止损/撤单配置」早返即止（不触持仓/下单链路）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading.stop_loss_context import StopLossContext


def test_frozen_rejects_field_assignment():
    """frozen：写字段抛 FrozenInstanceError——扩字段必须改 dataclass 本体（评审可见）。"""
    ctx = StopLossContext(stop_prices={"600000.SH": 9.5})
    with pytest.raises(Exception) as ei:  # FrozenInstanceError（dataclasses 版本间基类不同，宽断言）
        ctx.stop_prices = {}
    assert "frozen" in type(ei.value).__name__.lower() or "attribute" in str(ei.value).lower()


def test_defaults_all_none():
    """缺省构造：三 map 全 None（monitor 侧 None=各自路径关闭，与旧 kwargs 缺省一致）。"""
    ctx = StopLossContext()
    assert ctx.stop_prices is None
    assert ctx.monitor_ctx is None
    assert ctx.pending_ctx is None


def test_field_roundtrip():
    """三 map 字段原样回读（收口是纯数据装箱，不做任何变换/校验——语义留在 monitor）。"""
    ctx = StopLossContext(
        stop_prices={"600000.SH": 9.5},
        monitor_ctx={"600000.SH": {"state": {"stop": 9.5}, "cfg": {}}},
        pending_ctx={"600000.SH": 11.0})
    assert ctx.stop_prices == {"600000.SH": 9.5}
    assert ctx.monitor_ctx["600000.SH"]["state"]["stop"] == 9.5
    assert ctx.pending_ctx == {"600000.SH": 11.0}


def _run_monitor_bare(context) -> dict:
    """裸跑 stop_loss_monitor 到早返层：patch 盘中/网关/行情，不触持仓与下单链路。"""
    from trading.phases.stop_loss import stop_loss_monitor
    gw = MagicMock()
    gw._fetch_broker_positions = AsyncMock(return_value={})
    with patch("trading.phases.stop_loss.get_gateway", return_value=gw), \
         patch("trading.phases.stop_loss.calendar") as cal, \
         patch("trading.phases.stop_loss.qmt_market_data") as qmd:
        cal.is_intraday_session.return_value = True
        cal.is_trading_day.return_value = True
        qmd.get_quotes = AsyncMock(return_value={})
        return asyncio.run(stop_loss_monitor(context))


def test_monitor_empty_context_maps_to_noop():
    """空 dict 三 map → 解包归一 None → 走「均空 no-op」既定分支（engine 旧 ``or None`` 契约）。

    行为等价红线锚：旧调用 ``stop_prices={} or None → None`` 与新 ``context 内 {} → None``
    必须同走 no-op（两种空值形态在 monitor 各判据 ``is not None and len>0`` 下恒等）。
    """
    result = _run_monitor_bare(
        StopLossContext(stop_prices={}, monitor_ctx={}, pending_ctx={}))
    assert result["checked"] == 0
    assert "无止损/撤单配置" in result.get("reason", "")


def test_monitor_none_context_backward_compatible():
    """context=None（裸调）与空 context 行为等价——旧 ``monitor()`` 无参调用路径不破。"""
    result = _run_monitor_bare(None)
    assert result["checked"] == 0
    assert "无止损/撤单配置" in result.get("reason", "")
