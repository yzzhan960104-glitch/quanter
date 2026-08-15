# -*- coding: utf-8 -*-
"""BrokerProtocol：实盘券商网关契约（W2-H1 · master design §5.1 依赖反转 Protocol）。

物理定位：
    W2-H1 把 broker/qmt.py 按连接/IO/业务分四文件后，上层（engine / order_state /
    circuit_breaker / reconcile）需要一个**与具体券商实现解耦的窄契约**。本 Protocol
    从 QmtExecutionGateway 现行方法签名逐字抄出（不发明新抽象——只把「上层实际
    依赖的网关面」显式化），供上层面向契约编程：未来接第二家券商（或测试替身）时，
    满足本面即可无缝替换，无需反向改调用方。

为什么是 Protocol（PEP 544 typing.Protocol）而非 ABC：
    - 鸭子类型 + 零侵入：QmtExecutionGateway 现有类面【已天然满足】本 Protocol
      （方法名/签名逐字对齐），无需声明继承或注册——分层红线「逻辑只搬」零追加；
    - 运行时零开销：Protocol 仅作类型注解；runtime_checkable 装饰让 isinstance 在
      契约测试里可用（只校验方法存在性，不做签名/语义运行时拦截，保持极简）；
    - 测试友好：MagicMock / SimpleNamespace 替身按需满足面。

方法契约（与 broker.qmt.QmtExecutionGateway 现行签名逐字对齐，零改造）：
    submit_order(order) -> OrderResult：
        下单（side="buy"/"sell"、price=None 市价 / 有值限价）。断线/风控锁定窗口
        返 REJECTED（熔断拒单，绝不发废单）；柜台拒单 seq<0 返 REJECTED；底层
        异常/超时返 FAILED 不冒泡（上层状态机兜底）。
    cancel_order(order_id) -> OrderResult：
        按对外 order_id（seq-str）撤单；真实单号映射缺失（回报未到）返 FAILED
        引导上层短暂重试。rc==0 仅表「指令已发出」非终态（message 显式标注）。
    query_asset() -> dict：
        资金四字段 {account_id, cash, total_asset, market_value}；查询失败/未连接/
        锁定统一返 {}（circuit_breaker 按 equity 缺失跳过当日损失检查，跳过≠熔断）。
    query_orders(cancelable_only=False) -> list[dict]：
        当日委托（subscribe 失败时订单状态盲区的唯一回填路径）；state 字段是
        **OrderState 枚举**（与 _orders 内部世界同型，消费者勿转字符串）。降级返 []。
    query_trades() -> list[dict]：
        当日成交（盘后成交对账）；None/异常/锁定降级返 []。
    sync_positions(local_positions, tolerance=0.0) -> ReconciliationResult：
        持仓对账（= _fetch_broker_positions 语义的对外模板方法：拉券商全量持仓
        → 扁平化 → 与本地理论持仓 reconcile，返回结构化差异供风控决策）。QMT 实现
        在 broker.base.BaseExecutionGateway（模板方法），子类只供 _fetch_broker_positions。
    probe_account_status(*, timeout=5.0) -> (ok, detail)：
        客户端存活性主动探针（补 on_disconnected 僵死盲区）。ok=True 客户端应答了；
        ok=False 探针失败（异常/超时/None，detail 含原因）。
    set_order_update_callback(cb) -> None：
        注入上层异步回报回调钩子（钉钉报警 / State 持久化 / DB 写入）。**契约的
        一部分**（spec §5.1 原文）：cb 接收解析后的 dict、返回 Awaitable，由网关
        主线程 create_task 调度——C++ 回调线程只投递，副作用在主线程协程安全发生。

契约面边界（刻意最小集）：
    只列上层实际消费的方法；connect/disconnect（生命周期由 engine bootstrap
    编排，不经本面注入）、get_quote（行情走 broker.qmt_quote 独立面）、锁风控
    状态机（is_blocked/set_risk_halt——风控层直读具体网关）不纳入。Mock 网关
    （broker.mock.MockExecutionGateway）为回测件，不承诺本面（缺 query_orders/
    probe_account_status 等），勿对其断言 isinstance。

注：与 trading/protocols.py 的 ExecutionExecutor（孤儿契约）不同，本 Protocol
有活跃实现（QmtExecutionGateway）与活跃消费者链（engine/order_state），是
W2 依赖反转的主锚点。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Protocol, runtime_checkable

from broker.base import OrderResult
from trading.compute.types import OrderRequest

if TYPE_CHECKING:
    # 仅返回值注解用（__future__ annotations 下为惰性字符串），运行时零绑定——
    # 与 broker.base 同款防环范式（reconcile 顶部不反查本模块）。
    from trading.compute.reconcile import ReconciliationResult


@runtime_checkable
class BrokerProtocol(Protocol):
    """实盘券商网关契约（QmtExecutionGateway 天然满足；上层面向此面编程）。"""

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        """下单：断线/锁定 REJECTED、柜台拒单 REJECTED、异常/超时 FAILED 不冒泡。"""
        ...

    async def cancel_order(self, order_id: str) -> OrderResult:
        """撤单（对外 order_id=seq-str）：映射缺失返 FAILED 引导短暂重试。"""
        ...

    async def query_asset(self) -> dict[str, Any]:
        """资金四字段 dict；失败/未连接/锁定统一降级返 {}。"""
        ...

    async def query_orders(self, cancelable_only: bool = False) -> list[dict[str, Any]]:
        """当日委托列表（state=OrderState 枚举）；降级返 []。"""
        ...

    async def query_trades(self) -> list[dict[str, Any]]:
        """当日成交列表；降级返 []。"""
        ...

    async def sync_positions(
        self,
        local_positions: Mapping[str, float],
        tolerance: float = 0.0,
    ) -> ReconciliationResult:
        """持仓对账（= _fetch_broker_positions 语义）：券商全量持仓 vs 本地理论持仓。"""
        ...

    async def probe_account_status(self, *, timeout: float = 5.0) -> tuple[bool, str]:
        """客户端存活主动探针：(ok, detail)；ok=False 时 detail 含失败原因。"""
        ...

    def set_order_update_callback(self, cb: Any) -> None:
        """注入上层异步回报回调钩子（契约一部分，spec §5.1）。

        cb 签名：``Callable[[Mapping[str, Any]], Awaitable[None]]``（即
        broker.qmt_connection.OrderUpdateCallback）——注解 Any 而非直接引该别名，
        保持本契约模块对 broker.qmt_connection 仅类型级依赖（运行时零绑定）。
        """
        ...


__all__ = ["BrokerProtocol"]
