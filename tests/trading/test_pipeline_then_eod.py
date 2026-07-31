# -*- coding: utf-8 -*-
"""Task 6 · C-2 事件链编排 pipeline_then_eod 单测。

物理意图：取代「19:00 时钟赌博」的确定性事件链——
采集子进程 await wait() → 按策略声明 key 并集校验 freshness → 全绿跑 eod → brief。
本测试用 mock 隔离子进程/策略装配/freshness，只验编排顺序与门控：
  - 非交易日 → noop（不触达采集/eod）
  - 数据未就绪 → eod 不跑（不产废信号）
  - 数据就绪 → eod 跑一次
  - 多实验 → required_data_keys 取并集后逐个校验
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime


@pytest.mark.asyncio
async def test_non_trading_day_noop(monkeypatch):
    from trading.orchestrate.pipeline import pipeline_then_eod
    with patch("trading.calendar.is_trading_day", return_value=False):
        eng = MagicMock()
        await pipeline_then_eod(eng)
        # 不应触达采集/eod


@pytest.mark.asyncio
async def test_data_not_ready_no_eod(monkeypatch):
    from trading.orchestrate.pipeline import pipeline_then_eod
    from data.freshness import FreshnessResult
    today = datetime.now().strftime("%Y-%m-%d")
    with patch("trading.calendar.is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch("trading.orchestrate.pipeline.resolve_active", return_value=[]), \
         patch("trading.orchestrate.pipeline.check_freshness",
               return_value=FreshnessResult("daily", False, None, today, "缺")):
        proc = AsyncMock(); proc.wait.return_value = 1
        cse.return_value = proc
        eng = MagicMock()
        eng._eod = AsyncMock()
        await pipeline_then_eod(eng)
        eng._eod.assert_not_awaited()  # 数据未就绪不跑 eod


@pytest.mark.asyncio
async def test_data_ready_runs_eod(monkeypatch):
    from trading.orchestrate.pipeline import pipeline_then_eod
    from data.freshness import FreshnessResult
    today = datetime.now().strftime("%Y-%m-%d")
    with patch("trading.calendar.is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch("trading.orchestrate.pipeline.resolve_active", return_value=[]), \
         patch("trading.orchestrate.pipeline.check_freshness",
               return_value=FreshnessResult("daily", True, today, today, "PASS")):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        eng = MagicMock()
        eng._eod = AsyncMock()
        await pipeline_then_eod(eng)
        eng._eod.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_experiment_keys_union(monkeypatch):
    from trading.orchestrate.pipeline import pipeline_then_eod
    from data.freshness import FreshnessResult
    today = datetime.now().strftime("%Y-%m-%d")
    strat_a = MagicMock(); strat_a.required_data_keys = frozenset({"daily"})
    strat_b = MagicMock(); strat_b.required_data_keys = frozenset({"daily", "moneyflow"})
    class FakeExp:
        strategy_name = "x"; params = {}
    exps = [FakeExp(), FakeExp()]
    checked_keys = []
    def fake_cf(k, exp):
        checked_keys.append(k)
        return FreshnessResult(k, True, today, today, "ok")
    with patch("trading.calendar.is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch("trading.orchestrate.pipeline.resolve_active", return_value=exps), \
         patch("trading.orchestrate.pipeline.build_strategy", side_effect=[strat_a, strat_b]), \
         patch("trading.orchestrate.pipeline.check_freshness", side_effect=fake_cf):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        eng = MagicMock(); eng._eod = AsyncMock()
        await pipeline_then_eod(eng)
        assert set(checked_keys) == {"daily", "moneyflow"}
