# -*- coding: utf-8 -*-
"""trading.io.breaker — 熔断撤单动作（I/O 副作用壳 · 只搬运不判定）。

物理定位（Layer2 阶段5 · spec §3.5 五层第④层 io/）：
    ``cancel_all_open_orders`` 是【副作用动作】——遍历 gw._orders，对未终态单调
    ``gw.cancel_order``（券商 I/O）。它不做任何业务判定（「哪些是终态」是用 OrderState
    枚举集的纯集合判定，非止损/风控类业务 if），故归 io/。

迁移路径（strangler 铁律①）：
    trading/circuit_breaker.py（旧物理定义 · 与 check_daily_loss_limit 纯判定 re-export
    混居）──搬迁──▶ trading/io/breaker.py（本文件 · 副作用单源）
                  ◀──垫片 re-export── trading/circuit_breaker.py（保既有调用零改动）。

Why 单笔失败容忍（物理意图保留）：
    熔断路径往往伴随异常环境（断线恢复、柜台限流、流动性枯竭），单笔 cancel_order
    抛异常是常态；一旦因单笔失败中断循环，剩余未终态单将持续暴露敞口，彻底违背
    「熔断把所有口子堵上」的物理意图。故 try/except 包裹 + logger.exception 全量记录，
    尽最大努力撤完。
"""
from __future__ import annotations

import logging
from typing import Any

from trading.types import OrderState

logger = logging.getLogger(__name__)

# 终态订单集合（与 qmt_gateway.cleanup_orders 逐字同源）。
#
# Why 必须用 OrderState 枚举集，而不是字符串集：
# - 真实网关（QmtExecutionGateway）的 ``_orders`` 流水里，``rec["state"]`` 全部是
#   ``OrderState`` 枚举（由 ``_map_qmt_status`` 返回、或在 on_stock_trade/on_order_error/
#   on_cancel_error 直接赋枚举）。
# - 若此处用字符串集 ``{"FILLED", ...}``，则 ``OrderState.FILLED not in {...}`` 恒为
#   True（枚举≠字符串），导致已成交单也被判为「未终态」→ 调 cancel_order 撤已成交单
#   （柜台报错或 no-op，语义错乱，偏离「只撤未终态」物理意图）。
# - 与 qmt_gateway.cleanup_orders 保持同源集合，是规避此类隐性类型失配的硬约束。
_TERMINAL: frozenset[OrderState] = frozenset({
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
    OrderState.FAILED,
    OrderState.PARTIAL_CANCELLED,
})


async def cancel_all_open_orders(gw: Any) -> int:
    """撤销网关下所有未终态订单（熔断/断线/紧急停机时调）。

    参数：
        gw: 执行网关实例，需暴露 ``_orders: dict[str, dict]``（与
            QmtExecutionGateway 同口径）与 async ``cancel_order(order_id)``。

    返回：
        成功发起撤单的笔数（注意：是「发起」而非「已撤成功」，柜台回报
        最终态有滞后；调用方需结合后续 on_cancel_error/on_stock_order
        对账最终确认）。

    Why 必须容忍单笔失败：
        熔断路径往往伴随异常环境（断线恢复、柜台限流、流动性枯竭），单笔
        cancel_order 抛异常是常态而非偶发；一旦因单笔失败中断循环，剩余
        未终态单将持续暴露敞口，彻底违背「熔断把所有口子堵上」的物理意图。
        故采用 try/except 包裹 + logger.exception 全量记录，尽最大努力撤完。
    """
    orders = getattr(gw, "_orders", {}) or {}
    n = 0
    for oid, rec in list(orders.items()):
        if rec.get("state") not in _TERMINAL:
            try:
                await gw.cancel_order(oid)
                n += 1
            except Exception:
                # 单笔失败不中断：记录后继续撤下一笔，最终返回成功发起数。
                logger.exception("熔断撤单失败 oid=%s", oid)
    logger.warning("熔断撤单完成，共撤 %s 笔未终态单", n)
    return n
