# -*- coding: utf-8 -*-
"""W3：TradingEngine.bootstrap() 收口 7 步 I/O 初始化（C-2 scheduling-orchestration Task 5）。

物理意图：原 ``trading/__main__._run_forever`` 把「连接网关 + 注册回调 + position_book.init_db
+ state_store.init_store + _migrate_env_to_account」7 步内联在入口里。W3 把这段 I/O
初始化提取到 ``TradingEngine.bootstrap()``（async），让独立 ``__main__`` 与 uvicorn
server lifespan 复用同一段初始化代码（构造(零 I/O) → bootstrap(I/O init) → start(调度启动)
三段分离）。

测试覆盖：
  - 主路径：网关非空 → connect/set_order_update_callback/三个 init 全调，``eng._gw`` 被赋值。
  - 降级路径：网关 None → 不抛，``eng._gw`` 保持 None（dry_run 影子模式合法）。

Mock 策略：patch ``trading.engine.get_gateway``（模块级函数）+ ``trading.position_book.init_db``
+ ``trading.state_store.init_store`` + ``trading.state_store._migrate_env_to_account``
（bootstrap 内函数局部 import 这些子模块，故 patch 源模块路径）。
"""
import pytest
from unittest.mock import AsyncMock, patch
from trading.engine import TradingEngine


@pytest.mark.asyncio
async def test_bootstrap_inits_db_and_connects():
    eng = TradingEngine()
    with patch("trading.engine.get_gateway") as gg, \
         patch("trading.position_book.init_db") as pb, \
         patch("trading.state_store.init_store") as ss, \
         patch("trading.state_store._migrate_env_to_account") as mig:
        gw = AsyncMock()
        gg.return_value = gw
        await eng.bootstrap()
        gw.connect.assert_awaited_once()
        gw.set_order_update_callback.assert_called_once()
        pb.assert_called_once()
        ss.assert_called_once()
        mig.assert_called_once()
        assert eng._gw is gw


@pytest.mark.asyncio
async def test_bootstrap_no_gateway_degrades():
    eng = TradingEngine()
    with patch("trading.engine.get_gateway", return_value=None), \
         patch("trading.position_book.init_db"), \
         patch("trading.state_store.init_store"), \
         patch("trading.state_store._migrate_env_to_account"):
        await eng.bootstrap()  # 不抛
        assert eng._gw is None
