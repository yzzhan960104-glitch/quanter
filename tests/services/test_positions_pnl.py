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

from server.services import trading_service


# ----------------------------------------------------------------------------
# 场景 ①：avg_price + 现价可用 → 算浮盈
# ----------------------------------------------------------------------------
def test_get_positions_computes_pnl_from_avg_and_last():
    """avg_price=10.0 + 现价 11.0 + 持仓 100 → market_value=1100, pnl=+100。"""

    async def _run():
        # _fetch_broker_positions 返回的 QMT 形态：{sym: {volume, avg_price, ...}}
        positions = {
            "300001.SZ": {
                "volume": 100.0,
                "avg_price": 10.0,
                "open_price": 10.0,
                "yesterday_volume": 100,
            }
        }
        # get_quotes 形态：{sym: tick_dict 或 None}；正常时 tick_dict 含 last_price
        quotes = {"300001.SZ": {"last_price": 11.0}}

        gw = AsyncMock()
        # get_positions 入口校验 ``is_locked=False and _connected=True`` 才放行查询；
        # AsyncMock 默认子属性是 truthy MagicMock，会让 is_locked 也 truthy → 误判锁定，
        # 故显式置「已连接 + 未锁定」态（与 trading_service.get_status 同口径镜像）。
        gw.is_locked = False
        gw._connected = True
        gw._fetch_broker_positions = AsyncMock(return_value=positions)
        # query_asset 不在 get_positions 链路，但本场景断言不涉及总资产，无需 stub

        with patch("server.services.trading_service.get_gateway", return_value=gw), \
             patch(
                 "server.services.trading_service.qmt_market_data.get_quotes",
                 new=AsyncMock(return_value=quotes),
             ):
            result = await trading_service.get_positions()

        assert len(result) == 1
        pos = result[0]
        assert pos["symbol"] == "300001.SZ"
        assert pos["qty"] == 100.0
        # 浮盈计算契约：last × qty / (last - avg) × qty
        assert pos["market_value"] == 1100.0   # 11.0 × 100
        assert pos["pnl"] == 100.0             # (11.0 - 10.0) × 100

    asyncio.run(_run())


# ----------------------------------------------------------------------------
# 场景 ②：现价缺失（get_quotes 返 None） → pnl/market_value=None（盲价防御）
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

        with patch("server.services.trading_service.get_gateway", return_value=gw), \
             patch(
                 "server.services.trading_service.qmt_market_data.get_quotes",
                 new=AsyncMock(return_value=quotes),
             ):
            result = await trading_service.get_positions()

        assert result[0]["pnl"] is None          # 盲价防御
        assert result[0]["market_value"] is None  # 不用前一收盘猜市值
        # symbol/qty 仍可返（持仓真相不依赖行情）
        assert result[0]["symbol"] == "300001.SZ"
        assert result[0]["qty"] == 100.0

    asyncio.run(_run())
