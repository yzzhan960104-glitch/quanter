"""
broker/mock.py
==============
MockExecutionGateway —— BaseExecutionGateway 的内存 Mock 参考实现。

Layer2 阶段 3（剥 broker 模块）抽自 trading/execution_gateway.py。本模块是 broker
叶子包的一部分（零反向依赖 trading 编排）。

Mock 行为约定（与真实券商的差异，测试时需知晓）：
- submit_order 假设全额即时成交（跳过 PENDING→SUBMITTED→FILLED 链路），
  真实场景须由 OrderStateMachine 处理部分成交与超时；
- 不模拟滑点、不模拟撤单拒绝、不模拟断线，这些由真实子类负责；
- query_asset/get_quote（spec §3.3 新增基类抽象）返占位假数据：query_asset 返
  空 dict（无资金语义），get_quote 返 None（无行情语义）——与 QMT 的降级口径
  完全一致（None/{} 表「缺失降级」），让面向 BaseExecutionGateway 编程的上层
  （如 circuit_breaker）在 Mock 下自然走降级分支，无需特判。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from broker.base import BaseExecutionGateway, OrderResult
from trading.compute.types import OrderRequest
from trading.types.order_state import OrderState  # Layer2 follow-up #4c：改指 types 真身


class MockExecutionGateway(BaseExecutionGateway):
    """
    Mock 参考实现：用内存 dict 模拟券商持仓，可注入漂移用于测试对账逻辑。

    生产环境用 QmtExecutionGateway(BaseExecutionGateway) 替换——其底层对接
    xtquant（同步 API + 回调推送），子类内用 ``loop.run_in_executor`` 把同步
    调用包裹到线程池即可与异步基类契合；**xtquant 的具体函数签名、参数名、
    回调注册时机须以 QMT/迅投官方文档为准，本计划不臆造任何字段**。
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

    async def query_asset(self) -> dict[str, Any]:
        """Mock 资金查询：返空 dict（占位降级，对齐 QMT 的 query_asset 缺失语义）。

        Why 返 {} 而非假数值：Mock 不模拟资金账户，返 {} 让 circuit_breaker 等
        面向 BaseExecutionGateway 编程的上层自然走「资产缺失→跳过当日损失检查」
        降级分支（与 QMT query_asset 未连接/锁定时返 {} 同口径），无需特判 Mock。
        """
        return {}

    async def get_quote(self, symbol: str) -> Optional[Mapping[str, Any]]:
        """Mock 行情查询：返 None（占位降级，对齐 QMT 的 get_quote 缺失语义）。

        Why 返 None：Mock 不接 xtdata，返 None 让 risk_shield 涨跌停关 /
        stop_loss 现价检查等上层自然走「行情缺失→跳过」降级分支（与 QMT
        get_quote 在 xtdata 不可用时返 None 同口径）。
        """
        return None
