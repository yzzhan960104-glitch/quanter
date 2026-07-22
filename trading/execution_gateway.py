"""
trading/execution_gateway.py
============================
实盘执行抽象层。

职责切分：
- 本模块实现「持仓对账」纯函数 reconcile()：无副作用、无 I/O、可独立单测。
  用于把「本地系统理论持仓」与「券商真实持仓」比对，暴露敞口偏差——
  这是实盘风控的核心：drifted（数量漂移）、only_local（疑似未成交/丢单）、
  only_broker（疑似外部成交/手动单）三类差异各自指向不同的风险场景。
- 后续追加异步抽象基类 BaseExecutionGateway 与 Mock 参考实现。

设计哲学（CLAUDE.md Karpathy 极简原则）：对账逻辑用纯函数 + dataclass 平铺
实现，不引入事件/ORM 黑盒；向量化思路以单遍遍历并集 + 显式分类完成。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class PositionDrift:
    """单个标的的持仓偏差快照（不可变值对象）。"""

    symbol: str
    local_qty: float       # 本地系统记录的理论持仓
    broker_qty: float      # 券商真实持仓
    delta: float           # broker_qty - local_qty（正=券商多，负=券商少）


@dataclass(frozen=True)
class ReconciliationResult:
    """对账结果聚合。is_ok=True 当且仅当无任何漂移与单边差异。"""

    matched: list[PositionDrift]        # |delta| <= tolerance
    drifted: list[PositionDrift]        # |delta| > tolerance（数量漂移）
    only_local: list[PositionDrift]     # 券商无、本地有（疑似未成交/丢单）
    only_broker: list[PositionDrift]    # 券商有、本地无（疑似外部成交/手动单）
    max_abs_drift: float                # 全局最大绝对偏差（敞口红线监控用）
    is_ok: bool


def reconcile(
    local: Mapping[str, float],
    broker: Mapping[str, float],
    tolerance: float = 0.0,
) -> ReconciliationResult:
    """
    比对本地与券商持仓，返回分类差异。

    风险语义（Why 这么分类）：
    - drifted：数量漂移。实盘中最危险——本地以为成交 100 股，券商只记 90，
      可能是部分成交未回写、回调丢消息或断线期间漏单，直接导致敞口失真。
    - only_local：本地有、券商无。疑似订单未真正成交或丢单（网络超时后本地
      乐观记账），会让策略高估持仓、超额下单。
    - only_broker：券商有、本地无。疑似外部手工成交或另一进程下单，意味着
      本地策略对真实敞口一无所知，可能与之反向操作。

    边界与健壮性：
    - tolerance=0 表示零容忍（实盘默认），tolerance>0 仅用于容忍碎股/手续费
      舍入造成的微小差异，不应被滥用为掩盖 drift 的借口。
    - 标的并集为 local ∪ broker；只在一侧出现的标的归入 only_*，且其 delta
      即该侧持仓的全量（另一侧按 0 处理），仍纳入 max_abs_drift 统计。
    - 不对 NaN/None 做特殊处理——调用方应保证 Mapping 值为有限数值；传入 NaN
      会导致 abs(NaN)<=tolerance 为 False 而被归入 drifted，错误会被暴露
      而非静默吞掉，符合「显式优于隐式」。
    """
    matched: list[PositionDrift] = []
    drifted: list[PositionDrift] = []
    only_local: list[PositionDrift] = []
    only_broker: list[PositionDrift] = []
    max_abs = 0.0

    # 单遍遍历并集：O(n+m)，无嵌套循环，内存仅累积结果列表。
    for symbol in set(local) | set(broker):
        local_qty = float(local.get(symbol, 0.0))
        broker_qty = float(broker.get(symbol, 0.0))
        delta = broker_qty - local_qty
        max_abs = max(max_abs, abs(delta))
        drift = PositionDrift(symbol, local_qty, broker_qty, delta)

        # 注意判断顺序：先判单边（避免把 only_* 误归入 matched/drifted）。
        if symbol not in broker:
            only_local.append(drift)
        elif symbol not in local:
            only_broker.append(drift)
        elif abs(delta) <= tolerance:
            matched.append(drift)
        else:
            drifted.append(drift)

    # is_ok 仅看三类异常列表是否全空；matched 多寡不影响。
    is_ok = not drifted and not only_local and not only_broker
    return ReconciliationResult(matched, drifted, only_local, only_broker, max_abs, is_ok)


# ============================================================================
# 实盘执行网关抽象基类 + Mock 参考实现（Task 5）
# ============================================================================
from abc import ABC, abstractmethod

from trading.order_state import OrderState


@dataclass(frozen=True)
class OrderRequest:
    """下单请求（与具体券商解耦的最小契约）。

    Why 最小化：只保留策略层真正需要的语义字段；券商私有的「最小手数/报价
    方式/股东代码」等参数留到子类适配层补充，避免基类被 QMT/同花顺等差异化
    字段污染。
    """

    symbol: str
    qty: float
    side: str                              # "buy" / "sell"
    price: float | None = None             # None=市价；有值=限价
    order_id: str | None = None            # 由调用方透传的客户端单号


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
    async def _fetch_broker_positions(self) -> Mapping[str, float]:
        """子类实现：从券商拉取真实持仓 {symbol: qty}（模板方法的可变点）。"""

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
        """
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


class MockExecutionGateway(BaseExecutionGateway):
    """
    Mock 参考实现：用内存 dict 模拟券商持仓，可注入漂移用于测试对账逻辑。

    生产环境用 QMTExecutionGateway(BaseExecutionGateway) 替换——其底层对接
    xtquant（同步 API + 回调推送），子类内用 ``loop.run_in_executor`` 把同步
    调用包裹到线程池即可与异步基类契合；**xtquant 的具体函数签名、参数名、
    回调注册时机须以 QMT/迅投官方文档为准，本计划不臆造任何字段**。

    Mock 行为约定（与真实券商的差异，测试时需知晓）：
    - submit_order 假设全额即时成交（跳过 PENDING→SUBMITTED→FILLED 链路），
      真实场景须由 OrderStateMachine 处理部分成交与超时；
    - 不模拟滑点、不模拟撤单拒绝、不模拟断线，这些由真实子类负责。
    """

    def __init__(self, initial_broker_positions: Mapping[str, float] | None = None) -> None:
        # 券商侧持仓（可被测试注入初始漂移，模拟外部成交/丢单等失配场景）
        self._broker_positions: dict[str, float] = dict(initial_broker_positions or {})
        self._connected = False
        self._seq = 0  # 自增序号，仅用于生成 Mock 单号

    async def connect(self) -> None:
        # Mock 连接恒成功；真实子类在此建立 session、登录、订阅回报。
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def _fetch_broker_positions(self) -> Mapping[str, float]:
        # 返回副本，避免上层误改 Mock 内部状态（防御性拷贝）。
        return dict(self._broker_positions)

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        if not self._connected:
            # 未连接直接拒单，对应实盘中 session 失效不应静默下单。
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED, message="未连接")
        # Mock 假设全额成交（真实场景须经 OrderStateMachine 处理部分成交/超时）。
        delta = order.qty if order.side == "buy" else -order.qty
        self._broker_positions[order.symbol] = self._broker_positions.get(order.symbol, 0.0) + delta
        self._seq += 1
        return OrderResult(
            order_id=order.order_id or f"MOCK-{self._seq}",
            state=OrderState.FILLED,
            filled_qty=order.qty,
            avg_price=order.price,
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        # Mock 不支持撤已成交单，直接回 CANCELLED 终态（真实子类须查询当前状态）。
        return OrderResult(order_id=order_id, state=OrderState.CANCELLED, message="mock 撤单")


# ============================================================================
# 注：宏观一票否决网关（VetoedError / MacroAwareGateway）已于 B-7 修复时移除。
# 原实现（Task 14 宏观 CTA Epic 3）存在三重缺陷：① 不继承 BaseExecutionGateway；
# ② submit_order 同步签名 (order, regime) 与基类 async(order) 不兼容；③ 就地改写
# frozen OrderRequest.quantity 会抛 FrozenInstanceError。蔡森专精化（Phase 1）已删除
# 宏观 CTA 策略，当前唯一策略 caisen 为纯价量形态学、不消费 CreditRegime，该死代码
# 零生产接入（仅文档/单测引用）。按 YAGNI 删除；若重启宏观风控，须重新设计为正确的
# BaseExecutionGateway 子类（async submit_order、返回新 OrderRequest 而非就地改 frozen）。
# ============================================================================

