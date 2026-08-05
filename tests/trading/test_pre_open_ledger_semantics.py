# -*- coding: utf-8 -*-
"""A2：pre_open 台账 partial/failed 语义 + C-8 对 failed 重试（08-05 done 掩盖废单教训）。"""
from datetime import datetime

import pytest


async def _run_pre_open(monkeypatch, tmp_path, result):
    """跑 pre_open 包裹（mock _pre_open_impl），返回 (返回体, 台账行)。"""
    from trading import engine, job_ledger
    monkeypatch.setenv("TRADING_JOB_LEDGER_DB", str(tmp_path / "job.db"))
    job_ledger.init_db()

    async def fake_impl(date):
        return result

    monkeypatch.setattr(engine, "_pre_open_impl", fake_impl)
    out = await engine.pre_open("2026-08-05")
    row = None
    import sqlite3
    with job_ledger._connect() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT status, message FROM job_run WHERE job_name='pre_open' AND business_date='2026-08-05'"
        ).fetchone()
    return out, row


@pytest.mark.asyncio
async def test_pre_open_live_all_rejected_marks_failed(monkeypatch, tmp_path):
    """A2: live 有计划单但 0 成交 → 台账 failed，message 含 submitted=0（不再 done 掩盖）。"""
    out, row = await _run_pre_open(monkeypatch, tmp_path, {
        "submitted": 0, "rejected": 1, "total": 1, "mode": "live"})
    assert out["submitted"] == 0
    assert row["status"] == "failed"
    assert "submitted=0" in row["message"]
    assert "rejected=1" in row["message"]


@pytest.mark.asyncio
async def test_pre_open_partial_rejected_marks_done(monkeypatch, tmp_path):
    """A2: 部分成功（submitted>0）→ done（不是全废，C-8 不应重试）。"""
    _, row = await _run_pre_open(monkeypatch, tmp_path, {
        "submitted": 1, "rejected": 1, "total": 2, "mode": "live"})
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_pre_open_gate_reason_marks_skipped(monkeypatch, tmp_path):
    """A2: gate 未过（skipped/reason）→ 保持 skipped（补跑窗口内可重试）。"""
    _, row = await _run_pre_open(monkeypatch, tmp_path, {
        "submitted": 0, "mode": "live", "skipped": "数据未就绪"})
    assert row["status"] == "skipped"
    assert "数据未就绪" in row["message"]


@pytest.mark.asyncio
async def test_catchup_retries_failed_pre_open(monkeypatch, tmp_path):
    """A2: 台账 failed 在 C-8 窗口内必须重试（只有 running/done 才跳过）。"""
    from trading import catchup, job_ledger
    import trading.engine as engine_mod

    monkeypatch.setenv("TRADING_JOB_LEDGER_DB", str(tmp_path / "job.db"))
    job_ledger.init_db()
    job_ledger.finish_run("pre_open", "2026-08-05", "failed", "submitted=0/1")

    ran: list[str] = []

    async def fake_pre_open(date):
        ran.append(date)

    monkeypatch.setattr(engine_mod, "pre_open", fake_pre_open)
    monkeypatch.setattr(catchup.calendar, "is_trading_day", lambda date: True)
    monkeypatch.setattr(catchup.clock, "now", lambda: datetime(2026, 8, 5, 9, 30))
    monkeypatch.setattr(catchup.clock, "today", lambda: "2026-08-05")

    ok, note = await catchup._catchup_pre_open()
    assert ok is True
    assert ran == ["2026-08-05"]
