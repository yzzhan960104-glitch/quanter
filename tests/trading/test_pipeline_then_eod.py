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
    # patch 目标须落被测模块的命名空间（pipeline 顶部 ``from trading.calendar import is_trading_day``
    # 已把名字绑进 pipeline 命名空间，patch 源模块 trading.calendar 不会改变 pipeline 已绑的名字 →
    # 交易日下 patch 失效，会越过 no-op guard 触达 await engine._eod() 在 MagicMock 上抛 TypeError。
    with patch("trading.orchestrate.pipeline.is_trading_day", return_value=False):
        eng = MagicMock()
        await pipeline_then_eod(eng)
        # 不应触达采集/eod：非交易日应在 is_trading_day 判定处直接 return。
        # 用 assert_not_called（eng 是 MagicMock，_eod 是普通 MagicMock 子属性，
        # 无 assert_not_awaited；assert_not_called 对 MagicMock/AsyncMock 均适用）。
        eng._eod.assert_not_called()


@pytest.mark.asyncio
async def test_data_not_ready_no_eod(monkeypatch):
    from trading.orchestrate.pipeline import pipeline_then_eod
    from data.freshness import FreshnessResult
    today = datetime.now().strftime("%Y-%m-%d")
    # C-4 U3c 后：rc=1 表示采集失败（直接 raise _CriticalHalt，L1）。
    # 「数据未就绪」语义 = 采集成功（rc=0）但 freshness 校验不过（落了但内容/日期错位）。
    # 故本用例的 proc.wait 必须返 0，由 freshness ok=False 触发 CRITICAL 跳过 eod。
    with patch("trading.orchestrate.pipeline.is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch("trading.orchestrate.pipeline.resolve_active", return_value=[]), \
         patch("trading.orchestrate.pipeline.check_freshness",
               return_value=FreshnessResult("daily", False, None, today, "缺")):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        eng = MagicMock()
        eng._eod = AsyncMock()
        # A1（08-14）：事件链 eod 前置 regime gate——fake 放行（regime 语义单测在
        # test_engine_regime_gate.py / test_regime.py）
        eng._regime_gate = AsyncMock(return_value=(True, ""))
        await pipeline_then_eod(eng)
        eng._eod.assert_not_awaited()  # 数据未就绪不跑 eod


@pytest.mark.asyncio
async def test_data_ready_runs_eod(monkeypatch):
    from trading.orchestrate.pipeline import pipeline_then_eod
    from data.freshness import FreshnessResult
    today = datetime.now().strftime("%Y-%m-%d")
    with patch("trading.orchestrate.pipeline.is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch("trading.orchestrate.pipeline.resolve_active", return_value=[]), \
         patch("trading.orchestrate.pipeline.check_freshness",
               return_value=FreshnessResult("daily", True, today, today, "PASS")):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        eng = MagicMock()
        eng._eod = AsyncMock()
        # A1（08-14）：事件链 eod 前置 regime gate——fake 放行（regime 语义单测在
        # test_engine_regime_gate.py / test_regime.py）
        eng._regime_gate = AsyncMock(return_value=(True, ""))
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
    with patch("trading.orchestrate.pipeline.is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch("trading.orchestrate.pipeline.resolve_active", return_value=exps), \
         patch("trading.orchestrate.pipeline.build_strategy", side_effect=[strat_a, strat_b]), \
         patch("trading.orchestrate.pipeline.check_freshness", side_effect=fake_cf):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        eng = MagicMock(); eng._eod = AsyncMock()
        # A1（08-14）：事件链 eod 前置 regime gate——fake 放行（regime 语义单测在
        # test_engine_regime_gate.py / test_regime.py）
        eng._regime_gate = AsyncMock(return_value=(True, ""))
        await pipeline_then_eod(eng)
        assert set(checked_keys) == {"daily", "moneyflow"}


@pytest.mark.asyncio
async def test_pipeline_collect_failure_raises_critical_halt(monkeypatch):
    """采集子进程 rc!=0 → raise _CriticalHalt（T+1 计划失真，停调度）。"""
    import asyncio
    from trading.engine import _CriticalHalt
    from trading.orchestrate import pipeline as pl

    class _FakeProc:
        async def wait(self):
            return 1   # 采集失败
    async def _fake_exec(*a, **kw):
        return _FakeProc()
    monkeypatch.setattr(pl.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(pl, "is_trading_day", lambda d: True)
    monkeypatch.setattr(pl, "resolve_active", lambda: [])
    monkeypatch.setattr(pl, "expected_latest_trade_day", lambda now: "2026-07-31")

    eng = object()   # 占位 engine（raise 在调 engine._eod 之前，不会被触达）
    with pytest.raises(_CriticalHalt, match="采集子进程失败"):
        await pl.pipeline_then_eod(eng)


@pytest.mark.asyncio
async def test_scan_fail_triggers_repair_but_eod_runs(monkeypatch):
    """T13-B #1：scan FAIL（unjustified_gaps>0）→ subprocess repair 触发，eod 仍跑（不阻断）。

    物理意图：scan 检测历史缺口，FAIL 不阻断当日 eod（历史缺口与当日交易无关，
    blueprint §2.3）；仅触发异步 repair 子进程。
    """
    from data.freshness import FreshnessResult
    from trading.orchestrate import pipeline as pl

    today = datetime.now().strftime("%Y-%m-%d")

    class _FakeProc:
        async def wait(self):
            return 0  # 采集成功
    async def _fake_exec(*a, **kw):
        return _FakeProc()
    monkeypatch.setattr(pl.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(pl, "is_trading_day", lambda d: True)
    monkeypatch.setattr(pl, "resolve_active", lambda: [])
    monkeypatch.setattr(pl, "expected_latest_trade_day", lambda now: today)
    monkeypatch.setattr(pl, "check_freshness",
                        lambda k, exp: FreshnessResult(k, True, today, today, "ok"))
    # scan 返 unjustified>0（FAIL，触发 repair）
    monkeypatch.setattr("data.tools.scan_integrity.scan", lambda **kw: {
        "unjustified_gaps": 3, "unjustified_symbols": ["000001.SZ"],
        "total_symbols": 1, "total_gaps": 3, "gaps": [], "scan_range": [today, today]})
    # 拦截 repair 子进程（不真起子进程）
    popen_calls = []
    import subprocess as _sp
    monkeypatch.setattr(_sp, "Popen",
                        lambda *a, **kw: popen_calls.append((a, kw)) or MagicMock())
    # 拦截 brief 播报（避免真跑网络/钉钉致测试卡）
    monkeypatch.setattr("ops.brief_all.run_brief_all", AsyncMock())

    eng = MagicMock()
    eng._eod = AsyncMock()
    # A1（08-14）：事件链 eod 前置 regime gate——fake 放行（regime 语义单测在
    # test_engine_regime_gate.py / test_regime.py）
    eng._regime_gate = AsyncMock(return_value=(True, ""))
    await pl.pipeline_then_eod(eng)

    # scan FAIL → subprocess repair 触发
    assert len(popen_calls) == 1, "scan FAIL 应触发 repair 子进程"
    # eod 仍跑（scan FAIL 不阻断当日交易）
    eng._eod.assert_awaited()
