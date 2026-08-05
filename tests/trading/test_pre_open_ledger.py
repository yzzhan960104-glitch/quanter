# -*- coding: utf-8 -*-
"""C-8 V3：pre_open 台账包裹单测（running→done/skipped/failed）。

物理意图（spec §3.4）：cron（engine._pre_open）与启动补跑（trading.catchup）共用
模块级 pre_open；台账在此统一落——skipped 不算完成，补跑窗口内可重试。
"""
import pytest
from unittest.mock import AsyncMock, patch

from trading import job_ledger


@pytest.mark.asyncio
async def test_pre_open_gate_skip_records_skipped():
    """gate 未过（无计划等）→ 台账 skipped（不算完成，补跑可重试）。"""
    from trading.engine import pre_open
    fake_engine = AsyncMock()
    fake_engine._pre_open_gate = AsyncMock(return_value=(False, "无计划"))
    with patch("trading.engine._ACTIVE_ENGINE", fake_engine), \
         patch("trading.engine.get_gateway", return_value=None):
        result = await pre_open("2026-08-03")
    assert result["skipped"] == "无计划"
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "skipped"


@pytest.mark.asyncio
async def test_pre_open_no_plan_records_skipped():
    """计划不存在 → 台账 skipped。

    C2c：pre_open 直读 DB list_signals_with_meta_by_plan_date；返空 = 无计划 = skipped。
    """
    from trading.engine import pre_open
    fake_engine = AsyncMock()
    fake_engine._pre_open_gate = AsyncMock(return_value=(True, ""))
    with patch("trading.engine._ACTIVE_ENGINE", fake_engine), \
         patch("trading.engine.get_gateway", return_value=None), \
         patch("trading.engine._state_store.list_signals_with_meta_by_plan_date",
               return_value=[]):
        result = await pre_open("2026-08-03")
    assert result["reason"] == "无计划"
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "skipped"


@pytest.mark.asyncio
async def test_pre_open_success_records_done():
    """主流程正常完成（至少一只已确认 SIGNAL，挂单 0 成功）→ 台账 done。

    C2c：pre_open 直读 DB；空 signals 会返「无计划」skipped，故需种一只 SIGNAL
    让主流程走完（confirmed + has_order=False + insert_order 通过 + _submit 成功）。
    """
    from trading.engine import pre_open
    fake_engine = AsyncMock()
    fake_engine._pre_open_gate = AsyncMock(return_value=(True, ""))
    _signals = [{"symbol": "300214.SZ",
                 "order": {"symbol": "300214.SZ", "qty": 100, "side": "buy", "price": 10.0},
                 "formed_at": None, "stop_price": 9.0, "take_profit": 11.0, "max_wait": 5}]
    with patch("trading.engine._ACTIVE_ENGINE", fake_engine), \
         patch("trading.engine.get_gateway", return_value=None), \
         patch("trading.engine._cancel_all_open_orders",
               new=AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
         patch("trading.engine._scan_expired_positions", return_value=[]), \
         patch("trading.engine._submit",
               new=AsyncMock(return_value={"state": "SUBMITTED", "order_id": "seq1"})), \
         patch("trading.engine._state_store") as ss:
        ss.list_signals_with_meta_by_plan_date.return_value = _signals
        ss.build_trade_id.side_effect = lambda aid, sym, d: f"{aid}_{sym}_{d}"
        ss.get_account.return_value = AsyncMock()
        ss.get_latest_action.return_value = "CONFIRMED"
        ss.has_order.return_value = False
        ss.insert_order.return_value = None
        ss.update_order_state.return_value = None
        ss.insert_trade_event.return_value = None
        result = await pre_open("2026-08-03")
    assert result["submitted"] == 1
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "done"


@pytest.mark.asyncio
async def test_pre_open_exception_records_failed_and_raises():
    """未预期异常 → 台账 failed 后上抛（cron 路径由 _critical_guard 按 C-4 L1 停调度）。"""
    from trading.engine import pre_open
    fake_engine = AsyncMock()
    fake_engine._pre_open_gate = AsyncMock(side_effect=RuntimeError("DB 故障"))
    with patch("trading.engine._ACTIVE_ENGINE", fake_engine), \
         patch("trading.engine.get_gateway", return_value=None):
        with pytest.raises(RuntimeError, match="DB 故障"):
            await pre_open("2026-08-03")
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "failed"