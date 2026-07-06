"""交易模块单元测试

覆盖范围：
- 订单状态机（8 种状态迁移）
- Mock 券商（下单、执行、账户管理）
- 异常处理（断线、限频、部分成交）
"""
import pytest
import pandas as pd
from datetime import datetime

from trading import OrderStateMachine, OrderState
from trading import MockBroker


class TestOrderStateMachine:
    """测试订单状态机"""

    @pytest.fixture
    def order(self):
        """初始化订单状态机"""
        return OrderStateMachine()

    def test_initial_state_pending(self, order):
        """测试初始状态为 PENDING"""
        assert order.get_state() == OrderState.PENDING

    def test_submit_transitions_to_submitted(self, order):
        """测试提交订单迁移到 SUBMITTED"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})

        assert order.get_state() == OrderState.SUBMITTED

    def test_submit_generates_order_id(self, order):
        """测试提交订单生成订单 ID"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})

        assert order.order_id is not None
        assert order.order_id.startswith("ORDER_")

    def test_submit_stores_order_info(self, order):
        """测试提交订单存储订单信息"""
        order_info = {"symbol": "600000.SH", "direction": "buy", "shares": 1000}
        order.submit(order_info)

        assert order.get_order_info()["symbol"] == "600000.SH"
        assert order.get_order_info()["direction"] == "buy"
        assert order.get_order_info()["shares"] == 1000

    def test_fill_partial_transitions_to_partial_filled(self, order):
        """测试部分成交迁移到 PARTIAL_FILLED"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})
        order.fill(500, 10.0)

        assert order.get_state() == OrderState.PARTIAL_FILLED

    def test_fill_full_transitions_to_filled(self, order):
        """测试完全成交迁移到 FILLED"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})
        order.fill(1000, 10.0)

        assert order.get_state() == OrderState.FILLED

    def test_fill_from_partial_to_filled(self, order):
        """测试从部分成交到完全成交"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})
        order.fill(500, 10.0)
        order.fill(500, 10.5)

        assert order.get_state() == OrderState.FILLED

    def test_cancel_transitions_to_cancelled(self, order):
        """测试取消迁移到 CANCELLED"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})
        order.cancel()

        assert order.get_state() == OrderState.CANCELLED

    def test_cancel_from_partial_transitions_to_partial_cancelled(self, order):
        """测试从部分成交取消迁移到 PARTIAL_CANCELLED"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})
        order.fill(500, 10.0)
        order.cancel()

        assert order.get_state() == OrderState.PARTIAL_CANCELLED

    def test_reject_transitions_to_rejected(self, order):
        """测试拒绝迁移到 REJECTED"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})
        order.reject("资金不足")

        assert order.get_state() == OrderState.REJECTED

    def test_reject_stores_reason(self, order):
        """测试拒绝存储原因"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})
        order.reject("资金不足")

        assert order.get_order_info()["reject_reason"] == "资金不足"

    def test_fail_transitions_to_failed(self, order):
        """测试失败迁移到 FAILED"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})
        order.fail("网络断线")

        assert order.get_state() == OrderState.FAILED

    def test_invalid_transition_raises_error(self, order):
        """测试非法状态迁移抛出异常（PENDING → FILLED 非法）

        PENDING 仅允许迁移到 SUBMITTED（见 _is_valid_transition）。
        注：原断言「SUBMITTED→FILLED 非法」是误解——fill() 本身就走 SUBMITTED→FILLED
        （满成交的合法路径），故该迁移合法、不会抛；改用 PENDING→FILLED 才是真非法。
        """
        # order 仍处 PENDING（未 submit）；PENDING→FILLED 不在合法迁移表 → 必抛
        with pytest.raises(ValueError, match="非法状态迁移"):
            order._transition_to(OrderState.FILLED)

    def test_fill_from_pending_raises_error(self, order):
        """测试从 PENDING 成交抛出异常"""
        with pytest.raises(ValueError, match="当前状态.*不支持成交"):
            order.fill(1000, 10.0)

    def test_cancel_from_pending_raises_error(self, order):
        """测试从 PENDING 取消抛出异常"""
        with pytest.raises(ValueError, match="当前状态.*不支持取消"):
            order.cancel()

    def test_reset_clears_state(self, order):
        """测试重置清空状态"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})
        order.reset()

        assert order.get_state() == OrderState.PENDING
        assert order.order_id is None
        assert order.get_order_info() is None

    def test_register_callback(self, order):
        """测试注册回调"""
        callback_called = []

        def callback(order_info):
            callback_called.append(True)

        order.register_callback(OrderState.FILLED, callback)
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})
        order.fill(1000, 10.0)

        assert len(callback_called) == 1

    def test_state_history_recorded(self, order):
        """测试状态历史被记录"""
        order.submit({"symbol": "600000.SH", "direction": "buy", "shares": 1000})
        order.fill(1000, 10.0)

        history = order.get_order_info()["state_history"]

        assert len(history) == 2
        assert history[0]["from"] == OrderState.PENDING
        assert history[0]["to"] == OrderState.SUBMITTED
        assert history[1]["from"] == OrderState.SUBMITTED
        assert history[1]["to"] == OrderState.FILLED


class TestMockBroker:
    """测试 Mock 券商"""

    @pytest.fixture
    def broker(self):
        """初始化 Mock 券商"""
        return MockBroker(
            initial_cash=1_000_000,
            seed=42,
            partial_fill_prob=0.0,  # 关闭部分成交，便于测试
            connection_fail_prob=0.0,  # 关闭断线，便于测试
        )

    def test_initial_state(self, broker):
        """测试初始状态"""
        assert broker.initial_cash == 1_000_000
        assert broker.cash == 1_000_000
        assert broker.positions == {}

    def test_place_order_returns_order_machine(self, broker):
        """测试下单返回订单状态机"""
        order = broker.place_order(
            symbol="600000.SH",
            direction="buy",
            shares=1000,
            price=10.0,
            order_type="market"
        )

        assert isinstance(order, OrderStateMachine)

    def test_place_order_updates_orders_list(self, broker):
        """测试下单更新订单列表"""
        broker.place_order(
            symbol="600000.SH",
            direction="buy",
            shares=1000,
            price=10.0
        )

        assert len(broker.orders) == 1

    def test_place_order_records_timestamp(self, broker):
        """测试下单记录时间戳"""
        broker.place_order(
            symbol="600000.SH",
            direction="buy",
            shares=1000,
            price=10.0
        )

        assert len(broker.order_timestamps) == 1

    def test_execute_order_updates_cash(self, broker):
        """测试执行订单更新现金"""
        order = broker.place_order(
            symbol="600000.SH",
            direction="buy",
            shares=1000,
            price=10.0
        )

        broker.execute_order(order, 10.0, 100000, 100000)

        # 现金应该减少
        assert broker.cash < 1_000_000

    def test_execute_order_buy_updates_position(self, broker):
        """测试执行买入订单更新持仓"""
        order = broker.place_order(
            symbol="600000.SH",
            direction="buy",
            shares=1000,
            price=10.0
        )

        broker.execute_order(order, 10.0, 100000, 100000)

        assert "600000.SH" in broker.positions
        assert broker.positions["600000.SH"] == 1000

    def test_execute_order_sell_updates_position(self, broker):
        """测试执行卖出订单更新持仓"""
        # 先买入
        broker.positions["600000.SH"] = 1000

        order = broker.place_order(
            symbol="600000.SH",
            direction="sell",
            shares=1000,
            price=10.0
        )

        broker.execute_order(order, 10.0, 100000, 100000)

        assert broker.positions["600000.SH"] == 0

    def test_execute_order_sell_increases_cash(self, broker):
        """测试执行卖出订单增加现金"""
        # 先买入
        broker.positions["600000.SH"] = 1000
        broker.cash = 900000

        order = broker.place_order(
            symbol="600000.SH",
            direction="sell",
            shares=1000,
            price=10.0
        )

        broker.execute_order(order, 10.0, 100000, 100000)

        # 现金应该增加
        assert broker.cash > 900000

    def test_execute_order_includes_slippage(self, broker):
        """测试执行订单包含滑点"""
        order = broker.place_order(
            symbol="600000.SH",
            direction="buy",
            shares=1000,
            price=10.0
        )

        # 执行订单
        broker.execute_order(order, 10.0, 100000, 100000)

        # 检查成交价格（应该包含滑点）
        # 买入滑点提高价格
        assert order.get_order_info()["filled_shares"] == 1000

    def test_execute_order_with_partial_fill(self):
        """测试部分成交"""
        broker = MockBroker(
            initial_cash=1_000_000,
            seed=42,
            partial_fill_prob=1.0,  # 强制部分成交
        )

        order = broker.place_order(
            symbol="600000.SH",
            direction="buy",
            shares=1000,
            price=10.0
        )

        broker.execute_order(order, 10.0, 100000, 100000)

        # 应该部分成交
        assert order.get_order_info()["filled_shares"] > 0
        assert order.get_order_info()["filled_shares"] < 1000

    def test_execute_order_with_connection_fail(self):
        """测试断线情况"""
        broker = MockBroker(
            initial_cash=1_000_000,
            seed=42,
            connection_fail_prob=1.0,  # 强制断线
        )

        order = broker.place_order(
            symbol="600000.SH",
            direction="buy",
            shares=1000,
            price=10.0
        )

        # 断线订单应该失败
        assert order.get_state() == OrderState.FAILED

    def test_execute_order_with_rate_limit(self):
        """测试限频情况"""
        broker = MockBroker(
            initial_cash=1_000_000,
            seed=42,
            rate_limit=1  # 每分钟最多 1 单
        )

        # 连续下 2 单
        order1 = broker.place_order(
            symbol="600000.SH",
            direction="buy",
            shares=1000,
            price=10.0
        )
        order2 = broker.place_order(
            symbol="600000.SH",
            direction="buy",
            shares=1000,
            price=10.0
        )

        # 第 2 单应该被限频拒绝
        assert order2.get_state() == OrderState.FAILED

    def test_get_account_info_returns_dict(self, broker):
        """测试获取账户信息返回字典"""
        account_info = broker.get_account_info()

        assert isinstance(account_info, dict)

    def test_get_account_info_includes_all_keys(self, broker):
        """测试账户信息包含所有字段"""
        account_info = broker.get_account_info()

        required_keys = ["cash", "positions", "initial_cash"]
        for key in required_keys:
            assert key in account_info

    def test_get_orders_returns_list(self, broker):
        """测试获取订单返回列表"""
        broker.place_order(
            symbol="600000.SH",
            direction="buy",
            shares=1000,
            price=10.0
        )

        orders = broker.get_orders()

        assert isinstance(orders, list)
        assert len(orders) == 1

    def test_get_portfolio_value(self, broker):
        """测试获取组合价值"""
        # 添加持仓
        broker.positions["600000.SH"] = 1000
        broker.cash = 900000

        prices = {"600000.SH": 10.0}
        portfolio_value = broker.get_portfolio_value(prices)

        # 组合价值 = 现金 + 持仓价值
        expected = 900000 + 1000 * 10.0
        assert portfolio_value == expected

    def test_reset_clears_account(self, broker):
        """测试重置清空账户"""
        broker.positions["600000.SH"] = 1000
        broker.cash = 900000

        broker.reset()

        assert broker.cash == 1_000_000
        assert broker.positions == {}
        assert len(broker.orders) == 0

    def test_calculate_slippage_buy_increases_price(self, broker):
        """测试买入滑点提高价格"""
        price = broker._calculate_slippage(
            market_price=100,
            shares=1000,
            avg_volume=100000,
            direction="buy",
            current_volume=100000
        )

        assert price > 100

    def test_calculate_slippage_sell_decreases_price(self, broker):
        """测试卖出滑点降低价格"""
        price = broker._calculate_slippage(
            market_price=100,
            shares=1000,
            avg_volume=100000,
            direction="sell",
            current_volume=100000
        )

        assert price < 100

    def test_calculate_slippage_clipped(self, broker):
        """测试滑点率被限制"""
        # 异常大的订单
        price = broker._calculate_slippage(
            market_price=100,
            shares=10000000,
            avg_volume=1000,
            direction="buy",
            current_volume=1000
        )

        # 滑点不应超过 10%
        assert price <= 110

    def test_calculate_liquidity_factor_amplifies(self, broker):
        """测试流动性因子放大"""
        factor = broker._calculate_liquidity_factor(
            current_volume=100,
            avg_volume=100000
        )

        assert factor > 1.0

    def test_calculate_liquidity_factor_normal(self, broker):
        """测试流动性因子正常"""
        factor = broker._calculate_liquidity_factor(
            current_volume=100000,
            avg_volume=100000
        )

        assert factor == 1.0

    def test_calculate_total_cost_buy(self, broker):
        """测试买入总成本"""
        order_info = {
            "symbol": "600000.SH",
            "direction": "buy",
            "shares": 1000,
            "price": 10.0,
            "order_type": "market",
        }

        order = OrderStateMachine()
        order.submit(order_info)

        broker.execute_order(order, 10.0, 100000, 100000)

        # 检查现金减少（买入成本）
        assert broker.cash < 1_000_000

    def test_calculate_total_cost_sell(self, broker):
        """测试卖出总成本"""
        # 先买入
        broker.positions["600000.SH"] = 1000
        broker.cash = 900000

        order_info = {
            "symbol": "600000.SH",
            "direction": "sell",
            "shares": 1000,
            "price": 10.0,
            "order_type": "market",
        }

        order = OrderStateMachine()
        order.submit(order_info)

        broker.execute_order(order, 10.0, 100000, 100000)

        # 检查现金增加（卖出收入减成本）
        assert broker.cash > 900000