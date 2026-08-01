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
    """计划不存在 → 台账 skipped。"""
    from trading.engine import pre_open
    fake_engine = AsyncMock()
    fake_engine._pre_open_gate = AsyncMock(return_value=(True, ""))
    with patch("trading.engine._ACTIVE_ENGINE", fake_engine), \
         patch("trading.engine.get_gateway", return_value=None), \
         patch("trading.trading_plan.load_plan", return_value=None):
        result = await pre_open("2026-08-03")
    assert result["reason"] == "无计划"
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "skipped"


@pytest.mark.asyncio
async def test_pre_open_success_records_done():
    """主流程正常完成（空订单列表）→ 台账 done。"""
    from trading.engine import pre_open
    fake_engine = AsyncMock()
    fake_engine._pre_open_gate = AsyncMock(return_value=(True, ""))
    with patch("trading.engine._ACTIVE_ENGINE", fake_engine), \
         patch("trading.engine.get_gateway", return_value=None), \
         patch("trading.trading_plan.load_plan",
               return_value={"confirmed": True, "orders": []}):
        result = await pre_open("2026-08-03")
    assert result["submitted"] == 0
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