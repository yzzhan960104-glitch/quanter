"""
broker/base.py
==============
实盘执行网关抽象基类 + 订单结果类型（broker 叶子包的契约根）。

Layer2 阶段 3（剥 broker 模块）抽自 trading/execution_gateway.py。本模块只含：
- ``OrderResult``：下单/撤单结果 dataclass（复用 OrderState 状态机枚举）；
- ``BaseExecutionGateway``：实盘执行网关抽象基类（全异步）。

职责切分（design §3.3）：
- broker 是【干净叶子】（零反向依赖 trading 编排）：本模块只允许 import 标准库 /
  trading.types / trading.compute.*（dataclass 契约）/ data 行情清洗可能用——
  绝不 import trading.engine / orchestrate / signal_runner / risk_shield 等编排层。
- 【纯决策】reconcile / PositionDrift / ReconciliationResult / OrderRequest 仍留
  trading/compute/（风控对账语义，阶段2 已抽）。本基类的 ``sync_positions`` 模板
  方法经 ``from trading.compute.reconcile import reconcile`` 引用对账纯函数——
  broker → trading.compute 单向依赖（trading.compute 是 functional core，无反向风险）。

新增抽象（spec §3.3 缺口，相对原 BaseExecutionGateway）：
- ``async query_asset()``：查资金（原 QMT 独有，未上提基类 → 二期 circuit_breaker
  需要 equity 源，补抽象让 Mock/QMT 同契约）；
- ``async get_quote(symbol)``：单标的实时快照（原 qmt_market_data 模块级自由函数，
  补抽象让行情成为网关契约的一部分，而非散落模块函数——broker 作为执行+行情的
  统一接入面）。

设计哲学（CLAUDE.md Karpathy 极简原则）：对账纯逻辑用纯函数 + dataclass 平铺
实现，不引入事件/ORM 黑盒；向量化思路以单遍遍历并集 + 显式分类完成。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional

from trading.compute.types import OrderRequest
from trading.types.order_state import OrderState  # Layer2 follow-up #4c：改指 types 真身

if TYPE_CHECKING:
    # 仅类型注解用，运行时不 import（避开 broker.base → trading.compute.reconcile
    # → trading/__init__ eager 加载 QmtExecutionGateway → broker.qmt → broker.base
    # 的循环 import；reconcile 在 sync_positions 内部延迟 import）。
    from trading.compute.reconcile import ReconciliationResult


@dataclass(frozen=True)
class OrderResult:
    """下单/撤单结果，复用既有 OrderState 状态机契约。

    Why 复用 OrderState：实盘订单的状态迁移（PENDING→SUBMITTED→FILLED/
    PARTIAL_FILLED/CANCELLED/REJECTED）已由 trading.order_state.OrderStateMachine
    严格约束，网关层不应另造一套状态词汇，避免双源真理（dual source of truth）。
    """

    order_id: str
    state: OrderState
    filled_qty: float = 0.0
    avg_price: float | None = None
    message: str = ""


class BaseExecutionGateway(ABC):
    """
    实盘执行网关抽象基类（全异步）。

    拷问边界（CLAUDE.md 接口与状态机红线）：
    - submit_order/cancel_order 在子类实现时必须幂等可重试；部分成交
      （PARTIAL_FILLED）须经 OrderStateMachine 合法迁移，不得越权改状态。
    - sync_positions 是风控核心：先取券商真实持仓，再与本地理论持仓对账，
      返回 ReconciliationResult 供上层决策（差异超阈值 → 触发 notifier）。
    - 真实 QMT 适配由子类实现 _fetch_broker_positions 与底层下单；本基类
      **不含**任何券商 API 调用，杜绝幻觉参数（CLAUDE.md 事实审查）。

    为什么全异步：实盘 Tick/订单回调天然事件驱动，异步事件循环可统一承载
    行情推送、订单回报、定时对账三类 I/O，避免多线程竞态。

    新增抽象（Layer2 阶段3 · spec §3.3 缺口）：
    - query_asset：资金查询（原 QMT 独有，上提为基类契约）；
    - get_quote：单标的行情（原模块级自由函数，上提为基类契约，让 broker 包
      统一承载「下单 + 查持仓 + 查资金 + 实时行情」全执行域）。
    """

    @abstractmethod
    async def connect(self) -> None:
        """建立并保活券商连接（子类须含断线重连与限频退避策略）。"""

    @abstractmethod
    async def disconnect(self) -> None:
        """优雅断开，释放连接与会话资源。"""

    @abstractmethod
    async def submit_order(self, order: OrderRequest) -> OrderResult:
        """提交订单，返回含 OrderState 的结果。"""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> OrderResult:
        """撤单。已成交单应返回当前终态而非抛错（幂等语义）。"""

    @abstractmethod
    async def _fetch_broker_positions(self) -> Mapping[str, Any]:
        """子类实现：从券商拉取真实持仓（模板方法的可变点）。

        返回形态（T7 后双形态，消费者须 isinstance 防御）：
        - Mock/EMT 子类：{symbol: qty(float)}（老契约）
        - QMT 子类（T7 扩展）：{symbol: {volume, avg_price, open_price, yesterday_volume}}
        sync_positions 模板方法扁平化为 {symbol: float} 再传 reconcile（契约不变）；
        其他消费者按需读原始返回的扩展字段。
        """

    @abstractmethod
    async def query_asset(self) -> dict[str, Any]:
        """查询资金资产（spec §3.3 新增基类契约，原 QMT 独有）。

        返回结构（4 字段标准化，QMT/Mock 子类须对齐）::

            {"account_id": str, "cash": float, "total_asset": float, "market_value": float}

        Why 上提基类：二期 circuit_breaker.check_daily_loss_limit 需 equity 源
        （total_asset），此前卡在「无 equity 源」；上提抽象后 Mock/QMT 同契约，
        circuit_breaker 可面向 BaseExecutionGateway 编程，不再绑死 QMT。

        降级语义（QMT 真身已实现）：查询失败/未连接/锁定 → 返 ``{}``，让调用方
        按缺失降级（如 circuit_breaker 跳过当日损失检查），不抛错阻断主路径。
        """

    @abstractmethod
    async def get_quote(self, symbol: str) -> Optional[Mapping[str, Any]]:
        """查单标的实时快照（spec §3.3 新增基类契约，原模块级自由函数）。

        返回结构（broker.qmt_quote._normalize_tick_sync 契约）::

            {"last_price": float|None, "high_limit": float|None, "low_limit": float|None,
             "open": ..., "high": ..., "low": ..., "pre_close": ..., "volume": ...,
             "amount": ..., "ask_price": ..., "bid_price": ..., "ask_vol": ..., "bid_vol": ...}

        Why 上提基类：行情是执行域的一部分（risk_shield 涨跌停校验、stop_loss
        现价检查都依赖实时快照），原散落在 qmt_market_data 模块函数，剥出 broker
        后统一为网关契约。Mock 实现可返占位假数据供回测/CI；QMT 委托 broker.qmt_quote.get_quote。

        降级语义：行情不可用（xtdata 缺失/异常/标的不存在）→ 返 ``None``，调用方
        按缺失降级（risk_shield 跳过涨跌停关、stop_loss 跳过现价检查），不抛错。
        """

    async def sync_positions(
        self,
        local_positions: Mapping[str, float],
        tolerance: float = 0.0,
    ) -> ReconciliationResult:
        """
        对账模板方法（Template Method）：取券商持仓 → 与本地比对。

        Why 模板方法而非抽象：对账流程「拉取 → 比对 → 返回结构化差异」是
        跨所有券商不变的算法骨架，唯一变化点是「如何拉取券商持仓」，故把
        变化点下沉为 _fetch_broker_positions 抽象方法，骨架固化在基类，
        杜绝子类漏改对账逻辑或绕过 tolerance 红线。

        注：reconcile 真身留 trading/compute/reconcile.py（风控对账语义，
        Layer2 阶段2 抽 functional core）。本基类经单向 import 引用——broker
        → trading.compute（core 无反向依赖）。
        """
        # 延迟 import reconcile（避开模块加载期循环：broker.base → trading.compute
        # → trading/__init__ eager QmtExecutionGateway → broker.qmt → broker.base）。
        # reconcile 只在本方法被调用时需要，模块加载期无谓提前 import 反而致循环。
        from trading.compute.reconcile import reconcile

        broker_positions = await self._fetch_broker_positions()
        # T7 扁平化：QMT _fetch_broker_positions 返 {sym: {volume, avg_price, ...}}，
        # 对账只关心 volume，扁平化为 {sym: float} 再传 reconcile。
        # Why 保持 reconcile 契约：reconcile(local, broker) 双方都是 {sym: float}，
        # 改 reconcile 契约会波及 mock/EMT/所有调用方，违反「最小改动」——故在
        # sync_positions 这一层做扁平化适配，扩展字段（avg_price 等）供其他消费者
        # 按需读 _fetch_broker_positions() 原始返回。
        # Why isinstance 防御：Mock/EMT 的 _fetch_broker_positions 仍返 {sym: float}，
        # isinstance 判断 next(iter(values)) 是否 dict，是 → 扁平化，否 → 原样透传，
        # 兼容 QMT（dict）/ Mock+EMT（float）双形态。
        if broker_positions and isinstance(next(iter(broker_positions.values()), None), dict):
            broker_positions = {s: p["volume"] for s, p in broker_positions.items()}
        return reconcile(local_positions, broker_positions, tolerance)
