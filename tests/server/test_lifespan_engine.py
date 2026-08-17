# -*- coding: utf-8 -*-
"""Task 9（C-2 scheduling-orchestration 收口）：lifespan 装配 TradingEngine 单测。

物理意图（spec · task-9-brief Step 1）：
    engine 合并进 uvicorn 单进程后，``presentation/server/main.py`` 的 lifespan
    须在 ``yield`` 前装配 ``TradingEngine``（构造 + bootstrap + start——影子期闸已按
    ADR-16 修订移除 · 2026-08-17），在 shutdown 段优雅 shutdown。本测试用 mock engine
    验证：bootstrap + start 被调 + 退出时 shutdown 被调。

Why 用 ``unittest.mock.patch`` 而非真 engine：lifespan 装配块 try/except 包裹，
真 engine bootstrap 会连真实 QMT 网关 + 起 APScheduler（CI 无凭证 + 端口/线程污染），
单测必须 mock 掉 ``TradingEngine`` 构造来隔离。

TDD 约定：本仓库 pytest-asyncio 为 strict 模式（pytest.ini 未配 asyncio_mode），
显式 ``@pytest.mark.asyncio`` 装饰器触发异步测试收集（与 tests/ops/test_brief_all_async.py
同范式）。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_lifespan_assembles_engine(monkeypatch):
    """engine 装配：bootstrap + start 被调，lifespan 退出时 shutdown 被调。"""
    from presentation.server.main import lifespan

    eng = MagicMock()
    eng.sched.running = True            # shutdown 段据此判定是否调 shutdown
    eng.bootstrap = AsyncMock()

    app = MagicMock()
    app.state = MagicMock()

    with patch("trading.engine.TradingEngine", return_value=eng):
        async with lifespan(app):
            eng.bootstrap.assert_awaited_once()
            eng.start.assert_called_once()
        # lifespan 退出后：sched.running=True → shutdown 被调
        eng.shutdown.assert_called_once()


