# -*- coding: utf-8 -*-
"""持仓盈亏计算单测（Task 12 · 修 ``pnl=None`` G6）。

物理意图：
    现状 ``trading_service.get_positions`` 的 market_value/pnl 恒 None（第一版未查
    行情，仅返 symbol/qty）。本测试固化为两条契约：
      ① avg_price + 现价可用 → 计算 market_value=last×qty / pnl=(last-avg)×qty；
      ② 现价缺失（行情源 None） → pnl/market_value=None（盲价防御：绝不拿脏数据
         或前一收盘价「猜」浮盈，量化交易审计红线）。

asyncio 约定：
    本仓未启用 pytest-asyncio（见 pytest.ini / pyproject 无 asyncio_mode），
    按 Task8/10 同口径用 ``asyncio.run(...)`` 同步包装异步被测函数。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from trading import gateway_service as trading_service


# ----------------------------------------------------------------------------
# 场景 ①：avg_price + 现价可用 → 算浮盈
# ----------------------------------------------------------------------------

def test_get_positions_no_quote_pnl_none():
    """行情源对该标的返 None → pnl/market_value 必须为 None（不猜价）。"""

    async def _run():
        positions = {
            "300001.SZ": {
                "volume": 100.0,
                "avg_price": 10.0,
                "open_price": 10.0,
                "yesterday_volume": 100,
            }
        }
        # 关键：get_quotes 对该标的返 None（停牌/xtdata 异常/CI 无 xtquant 都会走此分支）
        quotes = {"300001.SZ": None}

        gw = AsyncMock()
        gw.is_locked = False
        gw._connected = True
        gw._fetch_broker_positions = AsyncMock(return_value=positions)

        with patch("trading.gateway_service.get_gateway", return_value=gw), \
             patch(
                 "trading.qmt_market_data.get_quotes",
                 new=AsyncMock(return_value=quotes),
             ):
            result = await trading_service.get_positions()

        assert result[0]["pnl"] is None          # 盲价防御
        assert result[0]["market_value"] is None  # 不用前一收盘猜市值
        # 盲价时：成本仍可透（broker.avg_price 不依赖行情），现价/盈亏率 None（算不出）
        assert result[0]["avg_price"] == 10.0
        assert result[0]["last_price"] is None
        assert result[0]["pnl_pct"] is None
        # symbol/qty 仍可返（持仓真相不依赖行情）
        assert result[0]["symbol"] == "300001.SZ"
        assert result[0]["qty"] == 100.0

    asyncio.run(_run())
