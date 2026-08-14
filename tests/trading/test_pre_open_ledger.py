# -*- coding: utf-8 -*-
"""C-8 V3：pre_open 台账包裹单测（running→done/skipped/failed）。

物理意图（spec §3.4）：cron（engine._pre_open）与启动补跑（trading.catchup）共用
模块级 pre_open；台账在此统一落——skipped 不算完成，补跑窗口内可重试。
"""
import pytest
from unittest.mock import AsyncMock, patch

from trading import job_ledger
from trading.ports import EnginePorts  # T1：构造 fake ports 驱动 pre_open gate


@pytest.mark.asyncio
async def test_pre_open_gate_skip_records_skipped():
    """gate 未过（无计划等）→ 台账 skipped（不算完成，补跑可重试）。"""
    from trading.engine import pre_open
    # T1：原 patch _ACTIVE_ENGINE → 改构造 fake EnginePorts 注入 gate 行为。
    fake_ports = EnginePorts(
        gate=AsyncMock(return_value=(False, "无计划")),
        whitelist_add=lambda syms: None,
        whitelist_clear=lambda: None)
    with patch("trading.phases.pre_open.get_gateway", return_value=None):
        result = await pre_open("2026-08-03", ports=fake_ports)
    assert result["skipped"] == "无计划"
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "skipped"


@pytest.mark.asyncio
async def test_pre_open_no_plan_records_skipped():
    """计划不存在 → 台账 skipped。

    C2c：pre_open 直读 DB list_signals_with_meta_by_plan_date；返空 = 无计划 = skipped。
    """
    from trading.engine import pre_open
    fake_ports = EnginePorts(
        gate=AsyncMock(return_value=(True, "")),
        whitelist_add=lambda syms: None,
        whitelist_clear=lambda: None)
    # W1-A/T2-Task18 B 类（属性级·保 engine）：_state_store 是共享 ``trading.state_store``
    # 模块对象（engine 与 phases.pre_open 顶部 ``from trading import state_store as`` 同指），
    # 属性级 patch 改的是模块对象属性，两路径都命中 → 无需迁（区别于整体替换 patch）。
    with patch("trading.phases.pre_open.get_gateway", return_value=None), \
         patch("trading.engine._state_store.list_signals_with_meta_by_plan_date",
               return_value=[]):
        result = await pre_open("2026-08-03", ports=fake_ports)
    assert result["reason"] == "无计划"
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "skipped"


@pytest.mark.asyncio
async def test_pre_open_success_records_done():
    """主流程正常完成（至少一只已确认 SIGNAL，挂单 0 成功）→ 台账 done。

    C2c：pre_open 直读 DB；空 signals 会返「无计划」skipped，故需种一只 SIGNAL
    让主流程走完（confirmed + has_order=False + insert_order 通过 + _submit 成功）。
    """
    from trading.engine import pre_open
    fake_ports = EnginePorts(
        gate=AsyncMock(return_value=(True, "")),
        whitelist_add=lambda syms: None,
        whitelist_clear=lambda: None)
    _signals = [{"symbol": "300214.SZ",
                 "order": {"symbol": "300214.SZ", "qty": 100, "side": "buy", "price": 10.0},
                 "formed_at": None, "stop_price": 9.0, "take_profit": 11.0, "max_wait": 5}]
    # W1-A/T2-Task18：pre_open 路径符号在 _pre_open_impl 函数体内读 phases.pre_open 顶部
    # import 的本地绑定（Task 4/6/7 切断 engine 反查后 patch trading.engine.X 全失效）→
    # 按「符号被读取时的 __globals__ 归属」迁物理路径 trading.phases.pre_open.X（Task 11/14 范式）。
    # _state_store 整体替换须迁 phases.pre_open._state_store：phases.pre_open 顶部
    # ``from trading import state_store as _state_store`` 为本地绑定，patch engine._state_store
    # 只改 engine 模块属性、phases.pre_open._state_store 仍指真 state_store → signals 读空
    # → submitted=0（本测 baseline fail 根因；整体替换不共享，区别于 test_no_plan 的属性级 patch）。
    with patch("trading.phases.pre_open.get_gateway", return_value=None), \
         patch("trading.phases.pre_open._cancel_all_open_orders",
               new=AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
         patch("trading.phases.pre_open._scan_expired_positions", return_value=[]), \
         patch("trading.phases.pre_open._submit",
               new=AsyncMock(return_value={"state": "SUBMITTED", "order_id": "seq1"})), \
         patch("trading.phases.pre_open._state_store") as ss:
        ss.list_signals_with_meta_by_plan_date.return_value = _signals
        ss.build_trade_id.side_effect = lambda aid, sym, d: f"{aid}_{sym}_{d}"
        ss.get_account.return_value = AsyncMock()
        ss.get_latest_action.return_value = "CONFIRMED"
        ss.has_order.return_value = False
        # G6 语义（2026-08-14）：pre_open 消费 insert_order 返回值——False/None=UNIQUE
        # 占位中止 _submit（防 DB/柜台脱节幽灵单）。本测构造「落库成功 + _submit 成功」
        # 走完主流程，mock 须返 True（旧 None 在 G6 下误判占位→submitted=0 假失败）。
        ss.insert_order.return_value = True
        ss.update_order_state.return_value = None
        ss.insert_trade_event.return_value = None
        result = await pre_open("2026-08-03", ports=fake_ports)
    assert result["submitted"] == 1
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "done"


@pytest.mark.asyncio
async def test_pre_open_exception_records_failed_and_raises():
    """未预期异常 → 台账 failed 后上抛（cron 路径由 _critical_guard 按 C-4 L1 停调度）。"""
    from trading.engine import pre_open
    fake_ports = EnginePorts(
        gate=AsyncMock(side_effect=RuntimeError("DB 故障")),
        whitelist_add=lambda syms: None,
        whitelist_clear=lambda: None)
    with patch("trading.phases.pre_open.get_gateway", return_value=None):
        with pytest.raises(RuntimeError, match="DB 故障"):
            await pre_open("2026-08-03", ports=fake_ports)
    assert job_ledger.latest_status("pre_open", "2026-08-03") == "failed"