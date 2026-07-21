# -*- coding: utf-8 -*-
"""安全熔断（日亏上限 + 撤未终态单补全）。

Why 独立模块：
- 一期 ``emergency_halt`` 只原子置 ``_lock_down``（拒新单）但**不撤已挂未成交单**
  ——断线/熔断时未终态单敞口失控，柜台仍可能继续推进成交，是实盘里最危险的
  「静默裸奔」状态（策略层以为已熔断、底层仍挂着单子）。
- 本模块补这一条路径：提供 ``check_daily_loss_limit``（日亏判定）与
  ``cancel_all_open_orders``（撤所有未终态单）两个工具函数，供二期引擎
  post_close（盘后）触发点与其他风险事件调用。

物理意图（Why 风控阈值定 -3%）：
- 日内 3% 权益回撤在 A 股单一策略层已属显著异常（多数交易日波动远小于此），
  一旦触及即视作「策略与环境失配」的强信号，宁可当日停手、次日复盘重启，
  也不容忍异常持续累积成穿仓。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from trading.order_state import OrderState

logger = logging.getLogger(__name__)

# 终态订单集合（与 qmt_gateway.cleanup_orders 逐字同源）。
#
# Why 必须用 OrderState 枚举集，而不是字符串集：
# - 真实网关（QmtExecutionGateway/EmtExecutionGateway）的 ``_orders`` 流水里，
#   ``rec["state"]`` 全部是 ``OrderState`` 枚举（由 ``_map_qmt_status`` 返回、
#   或在 on_stock_trade/on_order_error/on_cancel_error 直接赋枚举）。
# - 若此处用字符串集 ``{"FILLED", ...}``，则 ``OrderState.FILLED not in {...}``
#   恒为 True（枚举≠字符串），导致已成交单也被判为「未终态」→ 调 cancel_order
#   撤已成交单（柜台报错或 no-op，语义错乱，偏离「只撤未终态」物理意图）。
# - 与 qmt_gateway.cleanup_orders 保持同源集合，是规避此类隐性类型失配的硬约束。
_TERMINAL: frozenset[OrderState] = frozenset({
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
    OrderState.FAILED,
    OrderState.PARTIAL_CANCELLED,
})


def check_daily_loss_limit(
    start_equity: float,
    curr_equity: float,
    *,
    limit: float | None = None,
) -> bool:
    """判定日内权益回撤是否触及熔断上限。

    参数：
        start_equity: 当日开盘基线权益（如前一日收盘总资产）。
        curr_equity:  当前实时权益（盘中最新总资产）。
        limit:        负数熔断阈值，如 ``-0.03`` 表示亏 3% 即熔断；
                      None 则读 env ``CIRCUIT_DAILY_LOSS_LIMIT``，缺省 -0.03。

    返回：
        True 表示已触及/穿透熔断线，应进入熔断流程（lock_down + 撤单 + 告警）。

    边界：
    - ``start_equity <= 0`` 直接返回 False——既防除零，也表达「无有效基线权益
      时不应贸然触发熔断」（如冷启动首日未拿到准确基线，让引擎继续运行由
      其他维度兜底，避免除零异常使整条熔断链路失效）。
    - 采用 ``<=`` 而非 ``<``：恰触阈值即触发，风控宁可早一拍停手也不容忍
      边界继续裸奔（与 order_state.check_stop_loss 的判定口径对称）。
    """
    if limit is None:
        # env 缺省 -0.03：未显式配置时采用保守默认，避免线上裸奔。
        limit = float(os.getenv("CIRCUIT_DAILY_LOSS_LIMIT", "-0.03"))
    if start_equity <= 0:
        return False
    pnl_pct = (curr_equity - start_equity) / start_equity
    return pnl_pct <= limit


async def cancel_all_open_orders(gw: Any) -> int:
    """撤销网关下所有未终态订单（熔断/断线/紧急停机时调）。

    参数：
        gw: 执行网关实例，需暴露 ``_orders: dict[str, dict]``（与
            QmtExecutionGateway/EmtExecutionGateway 同口径）与 async
            ``cancel_order(order_id)``。

    返回：
        成功发起撤单的笔数（注意：是「发起」而非「已撤成功」，柜台回报
        最终态有滞后；调用方需结合后续 on_cancel_error/on_stock_order
        对账最终确认）。

    Why 必须容忍单笔失败：
    - 熔断路径往往伴随异常环境（断线恢复、柜台限流、流动性枯竭），单笔
      cancel_order 抛异常是常态而非偶发；一旦因单笔失败中断循环，剩余
      未终态单将持续暴露敞口，彻底违背「熔断把所有口子堵上」的物理意图。
    - 故采用 try/except 包裹 + logger.exception 全量记录，尽最大努力撤完。
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
