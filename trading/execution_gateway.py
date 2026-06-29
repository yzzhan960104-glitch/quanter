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
