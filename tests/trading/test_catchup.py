# -*- coding: utf-8 -*-
"""C-8 V4：启动补跑编排单测（判定/裁剪/顺序/brief 兜底/失败语义）。

物理意图（spec §3.3）：只补最近一致态——pipeline(D) 未 done 且其 18:00 已过才补；
plan 日期已过 pre_open 窗口 → run_eod=False 只补数据；pre_open 窗口
[09:22, ENGINE_PRE_OPEN_CATCHUP_UNTIL) 内且未 done 才补；失败 → failed+CRITICAL 不 raise。
"""
import pytest
from datetime import datetime, time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from trading import job_ledger


def _now(y, m, d, hh, mm):
    """固定 now（测试用，patch trading.clock.now 返回）。"""
    return datetime(y, m, d, hh, mm)


@pytest.mark.asyncio
async def test_pipeline_catchup_skipped_when_done(monkeypatch):
    """pipeline(D) 已 done → 不补跑。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 9, 40))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    job_ledger.begin_run("pipeline", "2026-07-31", "t")
    job_ledger.finish_run("pipeline", "2026-07-31", "done")
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["pipeline"] is False
    pl.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_catchup_runs_full_chain_in_window(monkeypatch):
    """窗口内（09:40）：pipeline(D) 补跑 run_eod=True，随后 pre_open 补挂。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 9, 40))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl, \
         patch("trading.engine.pre_open", new=AsyncMock()) as po:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["pipeline"] is True
    pl.assert_awaited_once()
    assert pl.await_args.kwargs == {"for_date": "2026-07-31", "run_eod": True}
    assert result["pre_open"] is True
    po.assert_awaited_once_with("2026-08-03")


@pytest.mark.asyncio
async def test_pipeline_catchup_run_eod_false_after_window(monkeypatch):
    """窗口已过（10:30）：run_eod=False 只补数据；pre_open 不补 + CRITICAL 知会。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 10, 30))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl, \
         patch("trading.engine.pre_open", new=AsyncMock()) as po, \
         patch("trading.catchup._alert_critical") as alert:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert pl.await_args.kwargs == {"for_date": "2026-07-31", "run_eod": False}
    assert result["pre_open"] is False
    po.assert_not_awaited()
    alert.assert_called_once()
    assert "窗口已过" in alert.call_args.args[0]


@pytest.mark.asyncio
async def test_pipeline_catchup_skipped_before_1800_same_day(monkeypatch):
    """D==today 且 now<18:00 → 不补跑（今晚 cron 将处理，防提前拉未清算数据）。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 16, 0))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-08-03")
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["pipeline"] is False
    pl.assert_not_awaited()


@pytest.mark.asyncio
async def test_weekend_catchup_produces_monday_plan(monkeypatch):
    """周六补跑：D=周五，plan_date=周一（>today）→ run_eod=True；pre_open 非交易日跳过。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 1, 10, 0))   # 周六
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: False)
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl, \
         patch("trading.engine.pre_open", new=AsyncMock()) as po:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert pl.await_args.kwargs == {"for_date": "2026-07-31", "run_eod": True}
    assert result["pre_open"] is False
    po.assert_not_awaited()


@pytest.mark.asyncio
async def test_brief_catchup_when_last_file_stale(monkeypatch, tmp_path):
    """pipeline(D) done 但 .last 文件缺失/陈旧 → run_brief_all 补播一次。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 9, 40))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    job_ledger.begin_run("pipeline", "2026-07-31", "t")
    job_ledger.finish_run("pipeline", "2026-07-31", "done")
    # 三个幂等文件重定向到 tmp：trading 陈旧、其余缺失 → 判定需补播
    files = {
        "trading": tmp_path / ".last_trading_brief",
        "strategy": tmp_path / ".last_strategy_brief",
        "data": tmp_path / ".last_data_brief",
    }
    files["trading"].write_text("2026-07-30", encoding="utf-8")
    def _fake_last(bot):
        return files[bot]
    with patch("broadcast.__main__.last_brief_file", side_effect=_fake_last), \
         patch("ops.brief_all.run_brief_all", new=AsyncMock()) as rba, \
         patch("trading.engine.pre_open", new=AsyncMock()) as po:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["brief"] is True
    rba.assert_awaited_once()
    po.assert_awaited_once_with("2026-08-03")   # brief 兜底不影响 pre_open


@pytest.mark.asyncio
async def test_catchup_failure_alerts_and_does_not_raise(monkeypatch):
    """补跑异常 → error 记录 + CRITICAL，不 raise（不停调度/不阻断 uvicorn）。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 9, 40))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    async def _boom(*a, **kw):
        raise RuntimeError("采集挂了")
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=_boom), \
         patch("trading.catchup._alert_critical") as alert:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["error"] == "采集挂了"
    alert.assert_called_once()


@pytest.mark.asyncio
async def test_pre_open_skipped_before_window(monkeypatch):
    """now<09:22 → pre_open 不补（09:22 cron 将处理），不告警。"""
    from trading import calendar as cal
    from trading import clock as clk
    monkeypatch.setattr(clk, "now", lambda: _now(2026, 8, 3, 9, 0))
    monkeypatch.setattr(cal, "expected_latest_trade_day", lambda now: "2026-07-31")
    monkeypatch.setattr(cal, "next_trading_day", lambda d: "2026-08-03")
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    with patch("trading.orchestrate.pipeline.pipeline_then_eod", new=AsyncMock()) as pl, \
         patch("trading.engine.pre_open", new=AsyncMock()) as po, \
         patch("trading.catchup._alert_critical") as alert:
        from trading.catchup import run_startup_catchup
        result = await run_startup_catchup(MagicMock())
    assert result["pre_open"] is False
    po.assert_not_awaited()
    alert.assert_not_called()
    pl.assert_awaited_once()   # pipeline 补跑不受窗口起点限制