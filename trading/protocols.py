# -*- coding: utf-8 -*-
"""执行器抽象接口（Layer2 阶段4 · spec §5 依赖反转 Protocol）。

物理定位：
    Layer2 阶段4 execution 包解散后，本 Protocol 由 execution/interfaces.py 迁入
    trading/protocols.py（spec §5 依赖反转 Protocol 归交易层）。ExecutionEngine
    （原 caisen 形态执行链）已在 Task 1.3 退役删除，本 Protocol 现为【孤儿契约】——
    无活跃消费者，但保留作为未来实盘 reducer 的依赖反转抽象（任何提供 get_status +
    submit_order 鸭子类型的对象均可注入：生产 server.trading_service / 测试 MagicMock）。

原 Step4d 物理意图（design §3.3 依赖反转 + CLAUDE.md 显式至上 / 拒绝黑盒）：
    把 ExecutionEngine 对 server.services.trading_service 的反向依赖【反转】为对
    本抽象接口 ``ExecutionExecutor`` 的依赖。

为什么是 Protocol（PEP 544 typing.Protocol）而非 ABC：
    - 鸭子类型 + 零侵入：server.trading_service 现有模块对象【已天然满足】此 Protocol
      （get_status / submit_order 签名一致），无需 trading_service 声明继承或注册——
      符合 4d「最小反转，不强重写 trading_service（实盘后续大改）」的用户决策。
    - 运行时零开销：Protocol 仅作类型注解，runtime_checkable 装饰只让 isinstance 在
      需要时可用，不强制运行时校验（注入路径不新增 isinstance 拦截，保持极简）。
    - 测试友好：MagicMock 自动满足任意 Protocol（属性/方法访问返回新 MagicMock），
      既有 tests/caisen/test_execution.py 全部 ``trading_service=MagicMock()`` 零改动。

调用面（ExecutionEngine 实际使用，Step1 审计结论·最小集）：
    - get_status() -> dict（同步）：返 {connected: bool, locked: bool, mode: str}，
      tick_pullback 判 locked/not-connected 跳过本轮断线保护；tick_exit 判 not-connected 跳过。
    - submit_order(order, *, dry_run, confirm) -> dict（async）：
      过 risk_shield.check_order 10 关风控 + 网关真单；返
      {order_id, state, message}，state ∈ {FILLED, PARTIAL_FILLED, REJECTED, FAILED,
      SUBMITTED, DRY_RUN, ...}。ExecutionEngine 据 state 推进状态机（仅真实成交才 FILLED/CLOSED）。

风控链不变性（strangler 红线·风控链不断）：
    ExecutionExecutor.submit_order 仍是【完整风控链】的入口——注入的生产实现
    server.trading_service.submit_order 内部调 trading.risk_shield.check_order（10 关
    短路：白名单/金额/股数/涨跌停/会话窗/锁定态/确认等）+ 网关真单 + emergency_halt
    锁定态感知。本 Protocol【不抽走也不弱化】风控逻辑，仅声明接口契约（实现侧不变）。
    emergency_halt 幂等 / T+1 底仓冻结 / locked 感知 全部保留在 trading_service 实现，
    4d 零改动（实盘逻辑后续大改时再统一收敛）。

注：本接口刻意保持【最小集】——只列 ExecutionEngine 当前调用的方法。未来若 ExecutionEngine
扩展调用面（如 get_positions），再追加方法签名（Protocol 增量演进，不破坏既有实现）。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ============================================================================
# ExecutionExecutor：执行器抽象（ExecutionEngine 依赖此接口，非 server 具体类型）
# ============================================================================
@runtime_checkable
class ExecutionExecutor(Protocol):
    """执行器契约：ExecutionEngine 依赖的【抽象执行器】（依赖反转 · DIP）。

    生产实现：``server.services.trading_service`` 模块（模块级函数 get_status /
    submit_order，模块对象本身鸭子类型满足本 Protocol——模块属性即方法）。
    测试桩：``MagicMock()`` / 自定义 fake trading 对象（tests/caisen/test_execution.py）。

    方法契约（与 server.trading_service 现有签名逐字对齐，零改造）：
        get_status() -> dict：
            同步返回交易通道四态 {connected, locked, mode}。
            - connected: 网关是否已连接（断线 → False，tick_* 据此跳过本轮防废单）；
            - locked: 风控否决/emergency_halt 锁定（tick_pullback 据此停新开仓；
              tick_exit【不】因此跳过——离场是风险缩减须持续）；
            - mode: unavailable/disconnected/live/vetoed_by_risk。
        submit_order(order, *, dry_run, confirm) -> dict（async）：
            下单编排：预取行情 → check_order 10 关风控 → 真单/模拟/拒单 → 落流水。
            返回 {order_id, state, message}；非 dry_run 挡板命中 raise RuntimeError。
            state 语义：FILLED/PARTIAL_FILLED（真实成交）/ REJECTED/FAILED（废单）/
            SUBMITTED（限价单排队）/ DRY_RUN（模拟）。
            order 类型：trading.execution_gateway.OrderRequest（execution 经延迟 import 引入）。
    """

    def get_status(self) -> dict:
        """交易通道四态探测：{connected: bool, locked: bool, mode: str}。"""
        ...

    async def submit_order(
        self,
        order: Any,
        *,
        dry_run: bool,
        confirm: bool,
    ) -> dict:
        """下单（过 risk_shield 10 关风控 + 网关真单）。返 {order_id, state, message}。

        order 类型注解为 Any（而非 OrderRequest）：避免本接口模块反向 import
        trading.execution_gateway（保持 execution.interfaces 零外部依赖，纯抽象）。
        ExecutionEngine 调用方在 tick_* 内延迟 import OrderRequest 构造 order 后注入。
        """
        ...


__all__ = ["ExecutionExecutor"]
