# -*- coding: utf-8 -*-
"""OrderStateMachine 状态迁移测试（应修项2：fail() 状态表 + None 守卫）。

覆盖节点（CLAUDE.md 量化风控·边界审查）：
  - fail() 声称「异常处理：任何状态 → FAILED」，但旧实现 PENDING→FAILED 被状态表
    拒绝、且 order_info=None 时崩溃（submit 前调用场景）。
  - 本测试锁定修正后的契约：非终态→FAILED 合法；order_info=None 惰性初始化不崩；
    终态（FILLED/CANCELLED/REJECTED）→FAILED 仍被拒（终态不可逆）。
"""
import pytest

from trading.order_state import OrderState, OrderStateMachine


def test_fail_from_pending_allowed():
    """PENDING→FAILED 必须合法（submit 前异常兜底，如网络/构造期失败）。

    旧实现：_is_valid_transition 的 PENDING 仅允许 SUBMITTED，fail() 抛「非法状态迁移」。
    """
    sm = OrderStateMachine()
    sm.fail("submit 前网络异常")
    assert sm.get_state() == OrderState.FAILED


def test_fail_with_none_order_info_no_crash():
    """order_info=None（submit 前调用）时 fail() 不应 TypeError。

    旧实现：self.order_info["fail_reason"] = reason → None 不可下标 → TypeError。
    """
    sm = OrderStateMachine()
    sm.fail("构造期异常")
    assert sm.get_state() == OrderState.FAILED


def test_fail_from_submitted_allowed():
    """SUBMITTED→FAILED 合法（下单后回报前的异常兜底）。"""
    sm = OrderStateMachine()
    sm.submit({"shares": 100})
    sm.fail("回报超时")
    assert sm.get_state() == OrderState.FAILED


def test_fail_records_reason():
    """fail() 应把 reason 记入 order_info（便于事后复盘失败原因）。"""
    sm = OrderStateMachine()
    sm.fail("资金不足")
    assert sm.get_order_info()["fail_reason"] == "资金不足"


def test_fail_from_terminal_rejected():
    """终态（FILLED/CANCELLED/REJECTED）→ FAILED 必须被拒（终态不可逆）。

    Why：已成交的单不能再标失败（会让风控/对账误判），状态机必须守住终态封闭性。
    """
    sm = OrderStateMachine()
    sm.submit({"shares": 100})
    sm.fill(100, 10.0)   # → FILLED（终态）
    with pytest.raises(ValueError, match="非法状态迁移"):
        sm.fail("不应从 FILLED 迁移到 FAILED")
    assert sm.get_state() == OrderState.FILLED   # 状态未变
