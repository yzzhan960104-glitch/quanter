# -*- coding: utf-8 -*-
"""C-8 V2：日期参数化 + pipeline 台账单测。

覆盖（spec §3.2/§3.4）：
  - pipeline_then_eod(for_date=D) → data_ready 落 D + engine._eod(data_day=D, plan_date=next(D))
  - run_eod=False → 不调 _eod，链尾 brief 仍跑
  - 默认路径（for_date=None）→ engine._eod() 无参（行为零变化）
  - 台账：成功 → done；data 未就绪 → failed；采集失败 → failed 后抛 _CriticalHalt
  - engine._eod(data_day/plan_date) → gate 用 data_day、eod_plan 落盘 key 用 plan_date
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from data.freshness import FreshnessResult
from trading import job_ledger


@pytest.mark.asyncio
async def test_for_date_passes_explicit_dates_to_eod():
    """for_date=D：data_ready 落 D，engine._eod 收 (data_day=D, plan_date=next(D))，链尾 brief 仍跑。"""
    from trading.orchestrate import pipeline as pl
    eng = MagicMock()
    eng._eod = AsyncMock()
    # A1（08-14）：事件链 eod 前置 regime gate——fake 放行（regime 语义单测在
    # test_engine_regime_gate.py / test_regime.py）
    eng._regime_gate = AsyncMock(return_value=(True, ""))
    with patch.object(pl, "is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch.object(pl, "resolve_active", return_value=[]), \
         patch.object(pl, "next_trading_day", return_value="2026-08-03"), \
         patch.object(pl, "check_freshness",
                      return_value=FreshnessResult("daily", True, "2026-07-31",
                                                   "2026-07-31", "PASS")), \
         patch.object(pl, "upsert_data_ready") as udr, \
         patch("ops.brief_all.run_brief_all", new=AsyncMock()):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        await pl.pipeline_then_eod(eng, for_date="2026-07-31")
    assert udr.call_args.args[0] == "2026-07-31"   # data_ready 落 for_date（非今天）
    assert eng._eod.await_args.kwargs == {"data_day": "2026-07-31", "plan_date": "2026-08-03"}
    assert job_ledger.latest_status("pipeline", "2026-07-31") == "done"


@pytest.mark.asyncio
async def test_default_path_calls_eod_without_args():
    """for_date=None（cron 正常路径）→ engine._eod() 无参，行为零变化。"""
    from trading.orchestrate import pipeline as pl
    eng = MagicMock()
    eng._eod = AsyncMock()
    # A1（08-14）：事件链 eod 前置 regime gate——fake 放行（regime 语义单测在
    # test_engine_regime_gate.py / test_regime.py）
    eng._regime_gate = AsyncMock(return_value=(True, ""))
    with patch.object(pl, "is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch.object(pl, "resolve_active", return_value=[]), \
         patch.object(pl, "check_freshness",
                      return_value=FreshnessResult("daily", True, "2026-08-03",
                                                   "2026-08-03", "PASS")), \
         patch("ops.brief_all.run_brief_all", new=AsyncMock()):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        await pl.pipeline_then_eod(eng)
    assert eng._eod.await_args.args == ()
    assert eng._eod.await_args.kwargs == {}


@pytest.mark.asyncio
async def test_run_eod_false_skips_eod_but_runs_brief():
    """run_eod=False（窗口已过只补数据）→ 不调 _eod，链尾 brief 仍跑，台账 done。"""
    from trading.orchestrate import pipeline as pl
    eng = MagicMock()
    eng._eod = AsyncMock()
    # A1（08-14）：事件链 eod 前置 regime gate——fake 放行（regime 语义单测在
    # test_engine_regime_gate.py / test_regime.py）
    eng._regime_gate = AsyncMock(return_value=(True, ""))
    with patch.object(pl, "is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch.object(pl, "resolve_active", return_value=[]), \
         patch.object(pl, "check_freshness",
                      return_value=FreshnessResult("daily", True, "2026-07-31",
                                                   "2026-07-31", "PASS")), \
         patch("ops.brief_all.run_brief_all", new=AsyncMock()) as rba:
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        await pl.pipeline_then_eod(eng, for_date="2026-07-31", run_eod=False)
    eng._eod.assert_not_awaited()
    rba.assert_awaited_once()
    assert job_ledger.latest_status("pipeline", "2026-07-31") == "done"


@pytest.mark.asyncio
async def test_data_unready_records_failed():
    """data 未就绪（采集成功但 freshness 不过）→ 台账 failed，eod 不跑。"""
    from trading.orchestrate import pipeline as pl
    eng = MagicMock()
    eng._eod = AsyncMock()
    # A1（08-14）：事件链 eod 前置 regime gate——fake 放行（regime 语义单测在
    # test_engine_regime_gate.py / test_regime.py）
    eng._regime_gate = AsyncMock(return_value=(True, ""))
    with patch.object(pl, "is_trading_day", return_value=True), \
         patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch.object(pl, "resolve_active", return_value=[]), \
         patch.object(pl, "check_freshness",
                      return_value=FreshnessResult("daily", False, None,
                                                   "2026-07-31", "缺")), \
         patch("ops.brief_all.run_brief_all", new=AsyncMock()):
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        await pl.pipeline_then_eod(eng, for_date="2026-07-31")
    eng._eod.assert_not_awaited()
    assert job_ledger.latest_status("pipeline", "2026-07-31") == "failed"


@pytest.mark.asyncio
async def test_collect_failure_records_failed_and_raises(monkeypatch):
    """采集子进程 rc!=0 → 台账 failed 后抛 _CriticalHalt（cron 路径 L1 停调度语义不变）。"""
    from trading.engine import _CriticalHalt
    from trading.orchestrate import pipeline as pl

    class _FakeProc:
        async def wait(self):
            return 1
    async def _fake_exec(*a, **kw):
        return _FakeProc()
    monkeypatch.setattr(pl.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(pl, "is_trading_day", lambda d: True)
    with pytest.raises(_CriticalHalt, match="采集子进程失败"):
        await pl.pipeline_then_eod(object(), for_date="2026-07-31")
    assert job_ledger.latest_status("pipeline", "2026-07-31") == "failed"


@pytest.mark.asyncio
async def test_eod_data_day_gate_uses_data_day(monkeypatch):
    """_eod(data_day=D)：交易日 gate 用 D（周末补跑周五链可过 gate）。"""
    from trading.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    seen = []
    monkeypatch.setattr("trading.engine.calendar.is_trading_day",
                        lambda d: seen.append(d) or True)
    monkeypatch.setattr("experiment.resolver.resolve_active", lambda: [])
    await eng._eod(data_day="2026-07-31", plan_date="2026-08-03")
    assert seen == ["2026-07-31"]


@pytest.mark.asyncio
async def test_eod_plan_date_flows_to_eod_plan(monkeypatch):
    """_eod(plan_date=P)：eod_plan 落盘 key 用 P（补跑产下一交易日计划）。"""
    from trading.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    monkeypatch.setattr("trading.engine.calendar.is_trading_day", lambda d: True)
    monkeypatch.setattr("experiment.resolver.resolve_active",
                        lambda: [MagicMock(strategy_name="s", params={}, weight=1.0,
                                           experiment_id="e1")])
    monkeypatch.setattr("strategies.registry.build_strategy",
                        lambda *a, **kw: MagicMock())
    monkeypatch.setattr("pandas.read_parquet", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("trading.engine._load_universe", lambda lake: [])
    monkeypatch.setattr("trading.engine._load_integrity_ctx", lambda d: (set(), set()))
    monkeypatch.setattr("data.integrity.filter_universe_by_continuity",
                        lambda *a, **kw: [])
    monkeypatch.setattr("trading.engine._resolve_id_window", lambda s: 10)
    monkeypatch.setattr("trading.engine._resolve_cooldown_days", lambda exps: 0)
    monkeypatch.setattr(eng, "_broadcast_positions_pnl", AsyncMock())
    eod_plan = AsyncMock()
    monkeypatch.setattr("trading.engine.eod_plan", eod_plan)
    await eng._eod(data_day="2026-07-31", plan_date="2026-08-03")
    assert eod_plan.await_args.args[0] == "2026-08-03"
    assert eod_plan.await_args.args[1] == []      # 无信号（universe 空）