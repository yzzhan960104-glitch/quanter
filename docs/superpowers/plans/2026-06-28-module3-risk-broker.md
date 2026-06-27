# 模块③ 风控 + Broker 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入轻量 Broker 抽象（回测内存撮合 / 实盘 QMT 接口）+ 独立 RiskManager（停牌涨跌停、单日开仓次数、个股占比 3 维度事前拦截），在引擎产出 Order 后、执行前强制风控；并物理删除已下线的单资产 `run()` 路径。

**Architecture:** `Strategy→Engine→RiskManager→Broker` 链在回测/实盘同构，仅 Broker 实现分叉。Broker 是薄抽象（直接操作 engine 账户状态，YAGNI 不引入 Account 框架）。RiskManager 横切在 `process_target_weight_signal` 的订单执行前，拒绝即记录原因丢弃。

**Tech Stack:** Python 3, pandas, numpy, pytest；复用 `backtest.engine.Order/OrderSide/BacktestEngine`、`factors.fusion.TargetWeightSignal`。

## Global Constraints

- 严禁 PyQt/GUI；全中文注释（含 Why）；扁平反黑盒，不引入风控/交易框架第三方库
- 风控拒绝 = 记录原因 + 丢弃（非异常），不中断回测
- RiskManager 日内开仓计数有状态，按交易日 `reset_daily()`；回测单实例串行无并发
- SELL 不受"开仓次数/占比上限"约束（减仓释放风险应放行），仅受停牌涨跌停约束
- **依赖模块①**（BacktestEngine.run_portfolio 已就绪）

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `trading/__init__.py` | 交易执行与风控包 | 新建 |
| `trading/broker.py` | `Broker` 抽象 + `FillResult` + `BacktestBroker` + `QmtBroker`(接口) | 新建 |
| `trading/risk_manager.py` | `RiskDecision` + `MarketState` + `RiskManager` | 新建 |
| `backtest/engine.py` | 接入 broker/risk_manager；`process_target_weight_signal` 扩展 `day_bars`；删除 `run()` 及单资产专用方法 | 修改 |
| `tests/test_trading.py` | 已存在，追加 broker/risk_manager 测试 | 修改 |
| `tests/test_backtest.py` | 迁移 `run()` 测试到 `run_portfolio` | 修改 |

---

## Task 1: `trading/broker.py`（Broker 抽象 + BacktestBroker）

**Files:**
- Create: `trading/__init__.py`、`trading/broker.py`
- Test: `tests/test_trading.py`（追加 `TestBacktestBroker`）

**Interfaces:**
- Consumes: `backtest.engine.Order/OrderSide/BacktestEngine`
- Produces: `Broker`（抽象）、`FillResult`、`BacktestBroker`、`QmtBroker`

- [ ] **Step 1: 写失败测试**

在 `tests/test_trading.py` 追加（顶部 import 区补）：
```python
import pandas as pd
from backtest.engine import BacktestEngine, Order, OrderSide
from trading.broker import Broker, BacktestBroker, QmtBroker, FillResult
```

```python
class TestBacktestBroker:
    """测试回测内存撮合 broker（成本模型移植自 _execute_portfolio_order）"""

    def _order(self, side, shares=100, price=4.0, symbol="510300.SH"):
        return Order(
            order_id="t1", symbol=symbol, side=side,
            shares=shares, price=price, timestamp=pd.Timestamp("2023-01-01"),
        )

    def test_buy_updates_position_and_cash(self):
        """买入：持仓增加、现金减少"""
        engine = BacktestEngine(initial_capital=1_000_000)
        broker = BacktestBroker()
        fill = broker.execute(self._order(OrderSide.BUY, shares=100, price=4.0), engine)

        assert fill.filled
        assert engine.positions_dict["510300.SH"] == 100
        assert engine.cash < 1_000_000   # 扣了成交金额 + 成本

    def test_sell_updates_position_and_cash(self):
        """卖出：持仓减少、现金增加"""
        engine = BacktestEngine(initial_capital=1_000_000)
        engine.positions_dict["510300.SH"] = 200
        broker = BacktestBroker()
        fill = broker.execute(self._order(OrderSide.SELL, shares=100, price=4.0), engine)

        assert fill.filled
        assert engine.positions_dict["510300.SH"] == 100
        # 卖出印花税使成本 > 买入，但现金仍净增
        assert engine.cash > 1_000_000 - 100 * 4.0 - 50

    def test_sell_has_stamp_duty_buy_does_not(self):
        """卖出收印花税，买入不收（成本差异）"""
        engine_buy = BacktestEngine(initial_capital=1_000_000)
        engine_sell = BacktestEngine(initial_capital=1_000_000)
        engine_sell.positions_dict["510300.SH"] = 100
        broker = BacktestBroker()

        fill_buy = broker.execute(self._order(OrderSide.BUY, shares=100, price=4.0), engine_buy)
        fill_sell = broker.execute(self._order(OrderSide.SELL, shares=100, price=4.0), engine_sell)

        # 同金额下，卖出成本（含印花税）应高于买入成本
        assert fill_sell.cost > fill_buy.cost

    def test_qmt_broker_not_implemented(self):
        """QmtBroker 本期仅留接口"""
        engine = BacktestEngine(initial_capital=1_000_000)
        broker = QmtBroker()
        with pytest.raises(NotImplementedError):
            broker.execute(self._order(OrderSide.BUY), engine)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_trading.py::TestBacktestBroker -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading.broker'`

- [ ] **Step 3a: 新建 `trading/__init__.py`**

```python
"""交易执行与风控模块

职责：
1. Broker 抽象（回测内存撮合 / 实盘 QMT 适配器）
2. RiskManager 事前风控拦截（停牌涨跌停、单日开仓次数、个股占比）

设计：薄抽象，不引入交易框架。回测/实盘走同一 Order→RiskManager→Broker 链。
"""
```

- [ ] **Step 3b: 新建 `trading/broker.py`**

```python
"""Broker 抽象与回测内存撮合实现

成本模型整体移植自 backtest/engine.py 原 _execute_portfolio_order：
- 佣金：成交金额 × 0.0003，最低 5 元
- 印花税：仅卖出，成交金额 × 0.0005
- 过户费：仅上海市场（代码 5/6 开头），成交金额 × 0.00001

BacktestBroker 直接操作传入的 engine 账户状态（cash/positions_dict）——
这是有意的薄耦合（YAGNI，不引入独立 Account 抽象）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from backtest.engine import Order, OrderSide


@dataclass
class FillResult:
    """订单成交结果"""
    filled: bool
    filled_shares: int
    avg_price: float
    cost: float
    reason: str = ""


class Broker(ABC):
    """执行层抽象：接收订单，更新账户状态，返回成交结果"""

    @abstractmethod
    def execute(self, order: Order, account) -> FillResult:
        """account 为 BacktestEngine 实例（回测）或实盘账户对象"""


class BacktestBroker(Broker):
    """回测内存撮合（无滑点，订单价全部成交）"""

    def execute(self, order: Order, account) -> FillResult:
        amount = order.shares * order.price

        # 佣金（万三，最低 5 元）
        commission = max(amount * 0.0003, 5.0)
        # 印花税（仅卖出，千五）
        stamp_duty = amount * 0.0005 if order.side == OrderSide.SELL else 0.0
        # 过户费（仅上海市场 5/6 开头，十万分之一）
        transfer_fee = amount * 0.00001 if order.symbol.startswith(("5", "6")) else 0.0
        total_cost = commission + stamp_duty + transfer_fee

        if order.side == OrderSide.BUY:
            account.cash -= (amount + total_cost)
            account.positions_dict[order.symbol] = (
                account.positions_dict.get(order.symbol, 0) + order.shares
            )
        else:
            account.cash += (amount - total_cost)
            account.positions_dict[order.symbol] = (
                account.positions_dict.get(order.symbol, 0) - order.shares
            )

        order.status = "filled"
        return FillResult(
            filled=True, filled_shares=order.shares,
            avg_price=order.price, cost=total_cost,
        )


class QmtBroker(Broker):
    """实盘 QMT 适配器（本期仅留接口，待后续接入 xtdata 下单）"""

    def execute(self, order: Order, account) -> FillResult:
        raise NotImplementedError("实盘 QMT 适配器本期仅留接口，待后续接入 xtdata")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_trading.py::TestBacktestBroker -v`
Expected: PASS — 4 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add trading/__init__.py trading/broker.py tests/test_trading.py
git commit -m "feat(trading): 新增 Broker 抽象与 BacktestBroker 内存撮合"
```

---

## Task 2: `trading/risk_manager.py`（3 维度事前拦截）

**Files:**
- Create: `trading/risk_manager.py`
- Test: `tests/test_trading.py`（追加 `TestRiskManager`）

**Interfaces:**
- Consumes: `backtest.engine.Order/OrderSide`
- Produces: `RiskDecision`、`MarketState`、`RiskManager`

- [ ] **Step 1: 写失败测试**

在 `tests/test_trading.py` 追加（import 区补）：
```python
from trading.risk_manager import RiskManager, RiskDecision, MarketState
```

```python
class TestRiskManager:
    """测试事前风控 3 维度拦截"""

    def _order(self, side=OrderSide.BUY, shares=1000, price=100.0, symbol="600000.SH"):
        return Order(
            order_id="t1", symbol=symbol, side=side,
            shares=shares, price=price, timestamp=pd.Timestamp("2023-01-01"),
        )

    def _tradable(self):
        return MarketState(is_tradable=True, is_limit_up_locked=False, is_limit_down_locked=False)

    def test_over_concentration_rejected(self):
        """维度③：买入后占比超 30% 拒绝"""
        rm = RiskManager(max_single_position_ratio=0.30)
        # 买入 1000×100=100000，AUM=200000 → 占比 50% > 30%
        decision = rm.check(
            self._order(shares=1000, price=100.0),
            account_aum=200_000, current_position_value=0,
            market_state=self._tradable(),
        )
        assert not decision.approved
        assert "占比" in decision.reason or "position" in decision.reason.lower()

    def test_within_concentration_approved(self):
        """维度③：占比内通过"""
        rm = RiskManager(max_single_position_ratio=0.30)
        # 1000×10=10000，AUM=1_000_000 → 占比 1%
        decision = rm.check(
            self._order(shares=100, price=10.0),
            account_aum=1_000_000, current_position_value=0,
            market_state=self._tradable(),
        )
        assert decision.approved

    def test_daily_new_position_limit_rejected(self):
        """维度②：单日开仓次数超限拒绝 BUY"""
        rm = RiskManager(max_daily_new_positions=2)
        rm._daily_buy_count = 2   # 已达上限
        decision = rm.check(
            self._order(OrderSide.BUY, shares=100, price=10.0),
            account_aum=1_000_000, current_position_value=0,
            market_state=self._tradable(),
        )
        assert not decision.approved

    def test_sell_not_limited_by_daily_count(self):
        """维度②：SELL 不受开仓次数约束"""
        rm = RiskManager(max_daily_new_positions=1)
        rm._daily_buy_count = 5
        decision = rm.check(
            self._order(OrderSide.SELL, shares=100, price=10.0),
            account_aum=1_000_000, current_position_value=5000,
            market_state=self._tradable(),
        )
        assert decision.approved

    def test_limit_up_blocks_buy(self):
        """维度①：一字涨停无法买入"""
        rm = RiskManager()
        locked = MarketState(is_tradable=True, is_limit_up_locked=True, is_limit_down_locked=False)
        decision = rm.check(
            self._order(OrderSide.BUY, shares=100, price=10.0),
            account_aum=1_000_000, current_position_value=0,
            market_state=locked,
        )
        assert not decision.approved

    def test_suspended_blocks_all(self):
        """维度①：停牌全拒"""
        rm = RiskManager()
        suspended = MarketState(is_tradable=False, is_limit_up_locked=False, is_limit_down_locked=False)
        decision = rm.check(
            self._order(OrderSide.SELL, shares=100, price=10.0),
            account_aum=1_000_000, current_position_value=5000,
            market_state=suspended,
        )
        assert not decision.approved

    def test_reset_daily_clears_counter(self):
        """reset_daily 重置日内开仓计数"""
        rm = RiskManager(max_daily_new_positions=1)
        rm._daily_buy_count = 1
        rm.reset_daily()
        assert rm._daily_buy_count == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_trading.py::TestRiskManager -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 写最小实现**

新建 `trading/risk_manager.py`：
```python
"""事前风控模块（借鉴 OSkhQuant khRisk.py）

3 维度拦截（在订单送交 broker 执行前）：
① 停牌/一字涨跌停过滤
② 单日最大开仓次数（仅约束 BUY）
③ 单只个股持仓资金占比上限（仅约束 BUY）

SELL 仅受维度①约束（减仓释放风险，应放行）。
日内开仓计数有状态，由引擎每日循环开始时 reset_daily() 重置。
"""
from dataclasses import dataclass

from backtest.engine import Order, OrderSide


@dataclass
class RiskDecision:
    """风控裁决"""
    approved: bool
    reason: str = ""


@dataclass
class MarketState:
    """单标的当日可交易状态

    回测用 OHLC 近似（一字板），实盘从 QMT 行情接口取。
    """
    is_tradable: bool              # 非停牌
    is_limit_up_locked: bool       # 一字涨停（无法买入）
    is_limit_down_locked: bool     # 一字跌停（无法卖出）


class RiskManager:
    def __init__(
        self,
        max_single_position_ratio: float = 0.30,
        max_daily_new_positions: int = 5,
        enable_market_filter: bool = True,
    ):
        self._max_single = max_single_position_ratio
        self._max_daily = max_daily_new_positions
        self._enable_filter = enable_market_filter
        self._daily_buy_count = 0   # 当日 BUY 计数，reset_daily 重置

    def reset_daily(self) -> None:
        """引擎每日循环开始时调用，重置日内开仓计数"""
        self._daily_buy_count = 0

    def check(
        self,
        order: Order,
        *,
        account_aum: float,
        current_position_value: float,
        market_state: MarketState,
    ) -> RiskDecision:
        """3 维度事前拦截"""
        # ① 停牌/一字涨跌停
        if self._enable_filter:
            if not market_state.is_tradable:
                return RiskDecision(False, "标停牌，不可交易")
            if order.side == OrderSide.BUY and market_state.is_limit_up_locked:
                return RiskDecision(False, "一字涨停，无法买入")
            if order.side == OrderSide.SELL and market_state.is_limit_down_locked:
                return RiskDecision(False, "一字跌停，无法卖出")

        # SELL 不受 ②③ 约束（减仓放行）
        if order.side == OrderSide.SELL:
            return RiskDecision(True)

        # ② 单日开仓次数
        if self._daily_buy_count >= self._max_daily:
            return RiskDecision(
                False, f"单日开仓次数达上限 {self._max_daily}"
            )

        # ③ 个股占比上限（下单后市值 / AUM）
        order_value = order.shares * order.price
        post_value = current_position_value + order_value
        if account_aum > 0 and post_value / account_aum > self._max_single:
            ratio = post_value / account_aum
            return RiskDecision(
                False, f"个股占比 {ratio:.1%} 超上限 {self._max_single:.0%}"
            )

        # 通过：BUY 计数 +1
        self._daily_buy_count += 1
        return RiskDecision(True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_trading.py::TestRiskManager -v`
Expected: PASS — 7 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add trading/risk_manager.py tests/test_trading.py
git commit -m "feat(trading): 新增 RiskManager 3 维度事前风控拦截"
```

---

## Task 3: `engine.py` 接入 Broker + RiskManager

**Files:**
- Modify: `backtest/engine.py`（`__init__` + `process_target_weight_signal` + `run_portfolio` + 新增辅助方法）
- Test: `tests/test_trading.py`（追加 `TestEngineRiskIntegration`）

**Interfaces:**
- Consumes: `Broker`/`BacktestBroker`、`RiskManager`（Task 1/2）
- Produces: `BacktestEngine(broker=, risk_manager=)`；`process_target_weight_signal(signal, prices, day_bars=None)`

- [ ] **Step 1: 写失败测试**

在 `tests/test_trading.py` 追加（import 区补）：
```python
from factors.fusion import TargetWeightSignal, SignalDirection
from trading.risk_manager import RiskManager, MarketState
from trading.broker import BacktestBroker
```

```python
class TestEngineRiskIntegration:
    """测试引擎接入风控+broker 后的端到端行为"""

    def _price_data(self, symbol="510300.SH", n=60):
        dates = pd.date_range("2023-01-01", periods=n, freq="D", tz="Asia/Shanghai")
        np.random.seed(1)
        prices = 4.0 + np.cumsum(np.random.randn(n) * 0.05)
        df = pd.DataFrame({
            "open": prices, "high": prices + 0.1, "low": prices - 0.1,
            "close": prices, "volume": 1e6, "amount": 1e7,
        }, index=dates)
        return {symbol: df}

    def test_engine_defaults_have_broker_and_risk(self):
        """引擎默认注入 BacktestBroker + RiskManager"""
        engine = BacktestEngine()
        assert engine.broker is not None
        assert engine.risk_manager is not None

    def test_portfolio_runs_with_risk_and_broker(self):
        """run_portfolio 经风控+broker 正常产出结果"""
        symbol = "510300.SH"
        price_data = self._price_data(symbol)
        engine = BacktestEngine(initial_capital=1_000_000)
        # 构造单日满仓信号
        signals = [
            TargetWeightSignal(
                timestamp=price_data[symbol].index[30],
                weights={symbol: 0.5},
                directions={symbol: SignalDirection.BUY},
            )
        ]
        result = engine.run_portfolio(price_data, signals)
        assert isinstance(result, dict)
        assert "daily_records" in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_trading.py::TestEngineRiskIntegration -v`
Expected: FAIL — `BacktestEngine` 无 `broker`/`risk_manager` 属性

- [ ] **Step 3a: 改 `BacktestEngine.__init__`**

在 `backtest/engine.py` 顶部 import 区补：
```python
from trading.broker import Broker, BacktestBroker
from trading.risk_manager import RiskManager, MarketState
```

把 `__init__` 签名（约第 97-102 行）改为：
```python
    def __init__(
        self,
        initial_capital: float = 1_000_000,
        cost_model: Optional[CostModel] = None,
        signal_freq: str = "1d",
        broker: Optional[Broker] = None,
        risk_manager: Optional[RiskManager] = None,
    ):
```

并在 `__init__` 体末尾（`self.portfolio_orders = []` 之后）补：
```python
        # 执行层 + 风控层（缺省注入，保证旧调用方无感升级）
        self.broker = broker if broker is not None else BacktestBroker()
        self.risk_manager = risk_manager if risk_manager is not None else RiskManager()
```

- [ ] **Step 3b: 改 `process_target_weight_signal` 签名 + 接入风控/broker**

把签名（约第 644-648 行）改为：
```python
    def process_target_weight_signal(
        self,
        signal: TargetWeightSignal,
        prices: Dict[str, float],
        day_bars: Optional[Dict[str, pd.Series]] = None,
    ) -> List[Order]:
```

把原步骤 5/6（约第 784-825 行，"执行卖出订单"与"执行买入订单"两段，以 `self._execute_portfolio_order` 调用为主）整体替换为：
```python
        # ============ 步骤 5/6：经风控 + broker 执行订单（卖出优先） ============
        for order in sell_orders + buy_orders:
            market_state = self._build_market_state(order.symbol, day_bars)
            decision = self.risk_manager.check(
                order,
                account_aum=self.calculate_aum(),
                current_position_value=self._position_value_of(order.symbol),
                market_state=market_state,
            )
            if not decision.approved:
                order.status = "rejected"
                self.portfolio_orders.append(order)
                self._record_failed_trade(
                    date=order.timestamp,
                    reason=f"风控拒绝: {decision.reason}",
                    shares=order.shares,
                    price=order.price,
                )
                continue

            # BUY 现金约束（保留原逻辑：不足则缩减或丢弃）
            if order.side == OrderSide.BUY:
                required_cash = order.shares * order.price
                estimated_cost = required_cash * 0.001 + 5.0
                if self.cash < required_cash + estimated_cost:
                    affordable = self._round_to_lot_size(
                        (self.cash - estimated_cost) / order.price
                    )
                    if affordable < 100:
                        order.status = "failed"
                        self.portfolio_orders.append(order)
                        self._record_failed_trade(
                            date=order.timestamp,
                            reason=f"资金不足：需要 {required_cash + estimated_cost:.2f}，"
                                   f"可用 {self.cash:.2f}",
                            shares=order.shares, price=order.price,
                        )
                        continue
                    order.shares = affordable

            # 执行（回测=BacktestBroker，实盘=QmtBroker）
            fill = self.broker.execute(order, self)
            if not fill.filled:
                self._record_failed_trade(
                    date=order.timestamp, reason=fill.reason or "成交失败",
                    shares=order.shares, price=order.price,
                )
                continue
            # 记录交易（broker 只更新账户，record 由引擎负责）
            self._record_trade(
                date=order.timestamp,
                direction="sell" if order.side == OrderSide.SELL else "buy",
                shares=fill.filled_shares, price=fill.avg_price,
                cost=fill.cost, symbol=order.symbol,
            )
```

并删除原 `_execute_portfolio_order` 方法（约第 827-885 行，其逻辑已移入 BacktestBroker）。

- [ ] **Step 3c: 新增辅助方法 + run_portfolio 传 day_bars/reset_daily**

在 `engine.py` 合适位置（如 `_generate_order_id` 之后）新增：
```python
    def _build_market_state(self, symbol: str,
                            day_bars: Optional[Dict[str, pd.Series]]) -> MarketState:
        """用当日 OHLC 近似一字涨跌停（day_bars 为空则全可交易）

        回测近似：开盘=高=收 → 一字涨停；开盘=低=收 → 一字跌停。
        实盘应由行情接口精确判定（含封单量），此处仅回测近似。
        """
        if not day_bars or symbol not in day_bars:
            return MarketState(is_tradable=True, is_limit_up_locked=False,
                               is_limit_down_locked=False)
        bar = day_bars[symbol]
        o, h, l, c = bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close")
        limit_up = None not in (o, h, c) and o == h == c
        limit_down = None not in (o, l, c) and o == l == c
        return MarketState(is_tradable=True, is_limit_up_locked=limit_up,
                           is_limit_down_locked=limit_down)

    def _position_value_of(self, symbol: str) -> float:
        """单标的持仓市值（持仓股数 × 最新收盘价）"""
        shares = self.positions_dict.get(symbol, 0)
        price = self.latest_prices.get(symbol, 0.0)
        if not np.isfinite(price):
            price = 0.0
        return shares * price
```

把 `run_portfolio` 中调 `process_target_weight_signal` 处（约第 971-972 行）改为传入 `day_bars`，并在每日循环开始（约第 947 行 `for date in all_dates:` 之后）加 `reset_daily`：
```python
        for date in all_dates:
            self.risk_manager.reset_daily()   # 每日重置风控日内计数
            # ============ 更新最新收盘价 ============
            ...
            if date in signal_index:
                signal = signal_index[date]
                trade_prices = {...}   # 保持原构造
                # 构造当日完整 OHLC 供风控判定涨跌停
                day_bars = {}
                for symbol in signal.weights.keys():
                    if symbol in price_data and date in price_data[symbol].index:
                        day_bars[symbol] = price_data[symbol].loc[date]
                if trade_prices:
                    self.process_target_weight_signal(signal, trade_prices, day_bars)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_trading.py::TestEngineRiskIntegration -v`
Expected: PASS

- [ ] **Step 5: 回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿（确认接入未破坏现有回测）

- [ ] **Step 6: 提交**

```bash
git add backtest/engine.py tests/test_trading.py
git commit -m "feat(engine): 接入 Broker+RiskManager，process_target_weight_signal 扩展 day_bars"
```

---

## Task 4: 物理删除 `engine.run()` 及单资产专用方法 + 迁移 `test_backtest.py`

**Files:**
- Modify: `backtest/engine.py`（删 `run`/`_calculate_target_position`/`_execute_trade`/`_update_daily_nav`/`_record_daily_state`/`_calculate_result` 等单资产专用方法；`_reset_state` 仅保留组合部分）
- Modify: `tests/test_backtest.py`（`TestBacktestEngine` 迁移到 `run_portfolio`）

**背景**：模块① 已让 `backtest_service`/`portfolio_service` 不再调用 `run()`。本任务完成 Series 路径的物理下线。`TestBacktestEngine`（test_backtest.py:321-428）11 个测试依赖 `run()`，需迁移。

**已知差异**：`_calculate_portfolio_result` 不计算 `win_rate`/`profit_loss_ratio`（现有 `.get(...,0)` 兜底），迁移时相关断言删除。

- [ ] **Step 1: 改写 `tests/test_backtest.py` 的 `TestBacktestEngine`**

把 `TestBacktestEngine` 类（第 321-428 行）整体替换为：
```python
def _signal_to_portfolio(symbol, df, signal):
    """测试 helper：把单资产 [0,1] 信号转为 run_portfolio 输入"""
    from factors.fusion import TargetWeightSignal, SignalDirection
    price_data = {symbol: df}
    signals = [
        TargetWeightSignal(
            timestamp=ts, weights={symbol: float(w)},
            directions={symbol: SignalDirection.BUY},
        )
        for ts, w in signal.items()
    ]
    return price_data, signals


class TestBacktestEngine:
    """测试回测引擎（迁移至 run_portfolio，Series 路径已下线）"""

    SYMBOL = "600000.SH"

    @pytest.fixture
    def engine(self):
        return BacktestEngine(initial_capital=1_000_000, signal_freq="1d")

    def test_initial_state(self, engine):
        """测试初始状态"""
        assert engine.initial_capital == 1_000_000
        assert engine.cash == 1_000_000
        assert engine.nav == 1_000_000

    def test_run_portfolio_returns_dict(self, engine, sample_df, sample_signal):
        """run_portfolio 返回字典"""
        price_data, signals = _signal_to_portfolio(self.SYMBOL, sample_df, sample_signal)
        result = engine.run_portfolio(price_data, signals)
        assert isinstance(result, dict)

    def test_run_portfolio_includes_core_fields(self, engine, sample_df, sample_signal):
        """run_portfolio 结果包含核心字段（无 win_rate/profit_loss_ratio）"""
        price_data, signals = _signal_to_portfolio(self.SYMBOL, sample_df, sample_signal)
        result = engine.run_portfolio(price_data, signals)
        for field in ["initial_capital", "final_nav", "total_return",
                      "annual_return", "max_drawdown", "sharpe_ratio",
                      "n_trades", "trades", "daily_records"]:
            assert field in result

    def test_max_drawdown_negative_or_zero(self, engine, sample_df, sample_signal):
        """最大回撤为负数或零"""
        price_data, signals = _signal_to_portfolio(self.SYMBOL, sample_df, sample_signal)
        result = engine.run_portfolio(price_data, signals)
        assert result["max_drawdown"] <= 0

    def test_n_trades_non_negative(self, engine, sample_df, sample_signal):
        """交易次数非负"""
        price_data, signals = _signal_to_portfolio(self.SYMBOL, sample_df, sample_signal)
        result = engine.run_portfolio(price_data, signals)
        assert result["n_trades"] >= 0

    def test_trades_and_daily_records_are_dataframes(self, engine, sample_df, sample_signal):
        """交易记录与每日记录为 DataFrame"""
        price_data, signals = _signal_to_portfolio(self.SYMBOL, sample_df, sample_signal)
        result = engine.run_portfolio(price_data, signals)
        assert isinstance(result["trades"], pd.DataFrame)
        assert isinstance(result["daily_records"], pd.DataFrame)

    def test_reset_state(self, engine):
        """测试重置状态"""
        engine.cash = 500000
        engine._reset_state()
        assert engine.cash == 1_000_000
```

- [ ] **Step 2: 运行测试确认失败（run() 已不存在）**

Run: `python -m pytest tests/test_backtest.py::TestBacktestEngine -v`
Expected: 改写后的测试应 PASS（此时 run() 尚在，但新测试用 run_portfolio）—— 本步先确认迁移测试本身可用

- [ ] **Step 3: 删除 `engine.py` 的单资产专用方法**

删除以下方法（删除整个方法体）：
- `run`（约第 132-198 行）
- `_calculate_target_position`（约第 214-237 行）
- `_execute_trade`（约第 239-347 行）
- `_update_daily_nav`（约第 349-364 行）
- `_record_daily_state`（约第 366-385 行）
- `_calculate_result`（约第 448-544 行）

**保留**（run_portfolio 仍用）：`_record_trade`、`_record_failed_trade`、`calculate_aum`、`calculate_current_weights`、`_round_to_lot_size`、`process_target_weight_signal`、`_build_market_state`、`_position_value_of`、`_generate_order_id`、`run_portfolio`、`_calculate_portfolio_result`。

把 `_reset_state`（约第 200-212 行）改为仅重置组合状态：
```python
    def _reset_state(self):
        """重置回测状态（组合模式；单资产 Series 路径已下线）"""
        self.cash = self.initial_capital
        self.nav = self.initial_capital
        self.trades = []
        self.daily_records = []
        self.positions = []
        self.positions_dict = {}
        self.latest_prices = {}
        self.portfolio_orders = []
```

并删除 `__init__` 中单资产专用属性（`self.position = 0`，约第 117 行）。

- [ ] **Step 4: 运行全部测试确认通过**

Run: `python -m pytest tests/ -v`
Expected: 全绿（run() 已删，所有测试走 run_portfolio）

- [ ] **Step 5: 提交**

```bash
git add backtest/engine.py tests/test_backtest.py
git commit -m "refactor(engine): 物理删除 run() 单资产路径，测试迁移至 run_portfolio"
```

---

## 验收标准

- [ ] `trading/broker.py` 含 Broker/FillResult/BacktestBroker/QmtBroker
- [ ] `trading/risk_manager.py` 含 RiskManager 3 维度拦截 + reset_daily
- [ ] `engine.process_target_weight_signal` 经 RiskManager.check → Broker.execute
- [ ] `engine.run()` 及单资产专用方法已删除；`test_backtest.py` 迁移至 run_portfolio 全绿
- [ ] `python -m pytest tests/ -v` 全绿
- [ ] 4 个独立 commit

## 后续衔接

引擎执行链已完整（策略→引擎→风控→broker）。下一份 plan 为模块⑤ QMT 解析器（独立数据源，可与模块④ 并行准备）。
