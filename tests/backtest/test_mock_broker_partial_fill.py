# -*- coding: utf-8 -*-
"""mock_broker 部分成交续成交测试（2026-09-09 死锁修复的回归钉）。

Why 此测试：execute_order 原守卫只放 SUBMITTED，部分成交置 PARTIAL_FILLED 后
剩余股数永无成交路——order_state 状态机支持 PARTIAL_FILLED→FILLED 迁移，但
撮合层拒绝再执行，资金/持仓停在半截（filled_shares 永远 < 下单量）。
"""
import pytest


def test_partial_fill_then_continuation_completes():
    from backtest.mock_broker import MockBroker
    from trading.types.order_state import OrderState

    broker = MockBroker(initial_cash=1_000_000.0, seed=7,
                        partial_fill_prob=1.0, connection_fail_prob=0.0)
    order = broker.place_order("300001.SZ", "buy", 1000, price=10.0,
                               order_type="limit")
    assert order.get_state() == OrderState.SUBMITTED

    # 第一轮：必部分成交（prob=1.0），filled ∈ (0, 1000)
    assert broker.execute_order(order, market_price=10.0,
                                current_volume=1e6, avg_volume=1e6)
    assert order.get_state() == OrderState.PARTIAL_FILLED
    info = order.get_order_info()
    first_leg = info["filled_shares"]
    assert 0 < first_leg < 1000
    assert broker.positions["300001.SZ"] == first_leg

    # 第二轮：续成交剩余股数（修复点——原实现此处抛 ValueError）
    assert broker.execute_order(order, market_price=10.0,
                                current_volume=1e6, avg_volume=1e6)
    assert order.get_state() == OrderState.FILLED
    info = order.get_order_info()
    assert info["filled_shares"] == 1000
    assert broker.positions["300001.SZ"] == 1000

    # 账户两腿合计记账：现金 = 初始 - 两腿金额 - 两腿佣金（无滑点遗漏/双计）
    p0, p1 = info["filled_price"]
    amounts = first_leg * p0 + (1000 - first_leg) * p1
    commissions = (max(first_leg * p0 * broker.commission_rate, broker.min_commission)
                   + max((1000 - first_leg) * p1 * broker.commission_rate,
                         broker.min_commission))
    assert broker.cash == pytest.approx(1_000_000.0 - amounts - commissions)


def test_terminal_state_execution_still_rejected():
    """终态单再 execute 仍拒绝（修复不放松终态封闭红线）。"""
    from backtest.mock_broker import MockBroker
    from trading.types.order_state import OrderState

    broker = MockBroker(initial_cash=1_000_000.0, seed=7,
                        partial_fill_prob=0.0, connection_fail_prob=0.0)
    order = broker.place_order("300001.SZ", "buy", 1000, price=10.0,
                               order_type="limit")
    assert broker.execute_order(order, market_price=10.0,
                                current_volume=1e6, avg_volume=1e6)
    assert order.get_state() == OrderState.FILLED
    with pytest.raises(ValueError):
        broker.execute_order(order, market_price=10.0,
                             current_volume=1e6, avg_volume=1e6)
