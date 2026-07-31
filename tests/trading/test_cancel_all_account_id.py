# -*- coding: utf-8 -*-
"""U5：pre_open 撤昨日单调 _cancel_all_open_orders 必须透传 account_id（激活 CANCELLED 回写）。

物理意图（spec §6.1 判据 · C-3 cancel 幂等审计收口）：
    breaker._cancel_via_broker_query 的柜台路径撤单回写 order.state=CANCELLED 是「条件激活」
    的——仅当 ``account_id`` 提供时才调 ``state_store.cancel_order_by_broker_oid_db``（breaker.py
    :120 ``if account_id:``）。pre_open 原调 ``_cancel_all_open_orders(gw)`` 未传 account_id →
    撤了昨日未成交单但 DB 仍记 SUBMITTED → T+1 对账幽灵单（以为没撤、重复挂或 FK 漂移）。

    修法（最小改动，不重建 C-1）：pre_open 改 ``_cancel_all_open_orders(gw, account_id=
    _resolve_account_id())`` 激活既有回写路径。审计结论：撤单落 DB → 免 purpose='CANCEL' 行
    （见 docs/superpowers/audits/2026-07-31-cancel-idempotency-audit.md）。

测试范式（沿袭 test_pre_open_l1_halt.py:15-17 TDD 约定）：
    本仓库未配 pytest-asyncio 的 asyncio_mode，历史 engine 测试一律 ``asyncio.run(...)``
    同步驱动 async。本测试沿袭该范式，避免引入 @pytest.mark.asyncio 造成风格分叉。

断言核心：
    spy 捕获 _cancel_all_open_orders 收到的 account_id 关键字参数 == _resolve_account_id() 返值。
    RED（修复前）：account_id 缺省 None → 断言失败。
    GREEN（修复后）：account_id 透传 → 断言通过。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


# ----------------------------------------------------------------------------
# 共享 fixture：隔离 env + DB + 构造 engine + 把 pre_open 走到撤单段的统一前置
# ----------------------------------------------------------------------------
@pytest.fixture
def isolated_eng(monkeypatch, tmp_path):
    """隔离环境 + 构造 TradingEngine + 注入 _ACTIVE_ENGINE 单例。

    与 test_pre_open_l1_halt.py 同款隔离，杜绝污染真实 .db / .json。
    """
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    from trading import state_store, position_book
    _db = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", _db)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", _db)
    position_book.init_db()
    state_store.init_store()

    from trading import engine
    eng = engine.TradingEngine()
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", eng)
    return eng


def test_pre_open_cancel_passes_account_id(isolated_eng, monkeypatch):
    """pre_open 调 _cancel_all_open_orders 时透传 account_id（激活柜台路径 CANCELLED 回写）。

    断言：spy 捕获的 account_id 关键字参数 == 'ACC_QMT_001'（_resolve_account_id 返值）。
    修复前 account_id=None → 柜台路径不回写 DB → 幽灵单；修复后透传 → 回写激活。
    """
    from trading import engine

    # gate 绿（撤单段在挂单循环之前，gate 是第一道）
    engine._ACTIVE_ENGINE._pre_open_gate = AsyncMock(return_value=(True, ""))

    captured: dict = {}

    async def _spy_cancel(gw, account_id=None):
        # spy：只捕获 account_id 关键字参数，不真撤单
        captured["account_id"] = account_id
        return {"cancelled": 0, "unconfirmed": 0}

    # 空 orders：让 pre_open 撤单段后走完挂单循环（无单可挂）快速返回。
    # 重点断言在 cancel spy，后续链路只要不抛即可。
    with patch("trading.engine.trading_plan") as tp, \
         patch("trading.engine.get_gateway", return_value=AsyncMock(**{"query_asset.return_value": {}})), \
         patch("trading.engine._cancel_all_open_orders", new=_spy_cancel), \
         patch("trading.engine._resolve_account_id", return_value="ACC_QMT_001"), \
         patch("trading.engine._load_expired_positions", return_value=[]):
        tp.load_plan.return_value = {"confirmed": True, "orders": []}
        asyncio.run(engine.pre_open("2026-07-31"))

    assert captured.get("account_id") == "ACC_QMT_001", (
        "pre_open 必须透传 account_id 激活柜台路径 cancel_order_by_broker_oid_db 回写 "
        "order.state=CANCELLED（消 T+1 对账幽灵单）。"
    )
