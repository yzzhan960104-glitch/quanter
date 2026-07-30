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


async def cancel_all_open_orders(gw: Any, account_id: str | None = None) -> dict[str, int]:
    """撤销网关下所有未终态订单（熔断/断线/紧急停机时调）。

    state-store-redesign §3.4/§4.4 改造：撤单数据源从 gw._orders 内存改为柜台查询。

    数据源优先级（P0-4 根因修复）：
        1. **柜台查询（优先）**：gw 暴露 query_orders + cancel_order_by_broker_oid 时，
           走 query_orders(cancelable_only=True) 查柜台全量可撤单，逐个 cancel_order_by_broker_oid
           撤（绕过 seq→real 映射——内存 oid 在 live 下是柜台真实单号，cancel_order 走映射必失败）。
        2. **内存回退（向后兼容）**：gw 无 query_orders 时（dry_run/老 Mock），退回遍历 gw._orders
           + cancel_order（旧路径，保既有调用零回归）。

    参数：
        gw: 执行网关实例。需暴露 _orders + async cancel_order（内存回退路径）；
            可选暴露 async query_orders(cancelable_only) + cancel_order_by_broker_oid(broker_oid)
            （柜台查询路径）+ async _confirm_cancelled(oid, timeout, interval)（撤单确认）。
        account_id: 所属账户（可选）。柜台路径撤单后回写 state_store.order.state=CANCELLED；
            None 时不回写 DB（仅撤单，不更新账本）。

    返回：
        ``{"cancelled": int, "unconfirmed": int}``（口径见下方 docstring）。

    Why 必须容忍单笔失败：
        熔断路径往往伴随异常环境（断线恢复、柜台限流、流动性枯竭），单笔
        cancel_order 抛异常是常态而非偶发；一旦因单笔失败中断循环，剩余
        未终态单将持续暴露敞口，彻底违背「熔断把所有口子堵上」的物理意图。
        故采用 try/except 包裹 + logger.exception 全量记录，尽最大努力撤完。

    Why 确认异常视为未确认（保守）：
        _confirm_cancelled 自身轮询也可能抛异常（query_orders 断线、柜台限流）；
        异常时 confirmed 置 False 而非 True，是「宁可误报未确认触发人工复核，
        不可漏报假装已撤」的保守取向——熔断场景下假撤单的代价（敞口残留）远
        高于误告警的代价（人工多看一眼）。
    """
    # 鸭子类型探针：T2 产出的确认方法（仅 QmtExecutionGateway 挂载）；
    # None 表示网关不支持确认（dry_run/老 Mock），退化为「不确认、unconfirmed 恒 0」。
    confirm_fn = getattr(gw, "_confirm_cancelled", None)
    query_fn = getattr(gw, "query_orders", None)
    cancel_by_oid_fn = getattr(gw, "cancel_order_by_broker_oid", None)

    # 路径选择：柜台查询优先（P0-4 根因修复），无 query_orders 回退内存
    if query_fn is not None and cancel_by_oid_fn is not None:
        return await _cancel_via_broker_query(
            gw, query_fn, cancel_by_oid_fn, confirm_fn, account_id)
    return await _cancel_via_memory(gw, confirm_fn)


async def _cancel_via_broker_query(gw, query_fn, cancel_by_oid_fn, confirm_fn,
                                   account_id: str | None) -> dict[str, int]:
    """柜台查询撤单路径（state-store-redesign §3.4 P0-4 根因修复）。

    查 query_orders(cancelable_only=True) 拿柜台全量可撤单，逐个 cancel_order_by_broker_oid
    撤（柜台单号直撤，绕过 seq→real 映射）。撤成后回写 state_store.order.state=CANCELLED（对账一致）。
    """
    from trading import state_store

    n_cancelled = 0
    n_unconfirmed = 0
    try:
        orders = await query_fn(cancelable_only=True)
    except Exception:
        logger.exception("query_orders 查柜台可撤单失败，撤单中止（无法得知可撤集合）")
        return {"cancelled": 0, "unconfirmed": 0}
    for o in orders or []:
        broker_oid = o.get("order_id")
        if broker_oid is None:
            continue
        try:
            await cancel_by_oid_fn(broker_oid)
            n_cancelled += 1
            # 回写 DB order.state=CANCELLED（account_id 提供时，对账一致）
            # 按 broker_oid 列更新（柜台单号，order_id 主键与 broker_oid 不同）
            if account_id:
                try:
                    state_store.cancel_order_by_broker_oid_db(str(broker_oid))
                except Exception:
                    logger.exception("回写 order.state=CANCELLED 失败 broker_oid=%s", broker_oid)
            if confirm_fn is not None:
                confirmed = True
                try:
                    confirmed = await confirm_fn(str(broker_oid), timeout=5.0, interval=0.5)
                except Exception:
                    confirmed = False
                    logger.exception("撤单确认异常（保守计未确认）broker_oid=%s", broker_oid)
                if not confirmed:
                    n_unconfirmed += 1
                    logger.warning(
                        "撤单未确认终态 broker_oid=%s（主推延迟或柜台未响应，需人工复核）", broker_oid)
        except Exception:
            logger.exception("熔断撤单失败 broker_oid=%s", broker_oid)
    logger.warning(
        "熔断撤单完成（柜台查询路径），共发起撤 %s 笔（其中未确认 %s 笔）",
        n_cancelled, n_unconfirmed)
    return {"cancelled": n_cancelled, "unconfirmed": n_unconfirmed}


async def _cancel_via_memory(gw, confirm_fn) -> dict[str, int]:
    """内存回退撤单路径（旧逻辑，向后兼容 dry_run/无 query_orders 的网关）。

    遍历 gw._orders，对未终态单调 gw.cancel_order（走 seq→real 映射）。
    保留旧行为：test_breaker_cancel_confirm.py / test_pre_open_cancels_yesterday_open_orders 仍走此路径。
    """
    orders = getattr(gw, "_orders", {}) or {}
    n_cancelled = 0
    n_unconfirmed = 0
    for oid, rec in list(orders.items()):
        if rec.get("state") not in _TERMINAL:
            try:
                await gw.cancel_order(oid)
                n_cancelled += 1
                # M2 撤单确认闭环：撤单「发起」成功后，轮询确认是否真到终态。
                # True=到终态；False=超时未确认 → 计 unconfirmed + WARNING，不假装撤成。
                if confirm_fn is not None:
                    confirmed = True
                    try:
                        confirmed = await confirm_fn(str(oid), timeout=5.0, interval=0.5)
                    except Exception:
                        confirmed = False
                        logger.exception("撤单确认异常（保守计未确认）oid=%s", oid)
                    if not confirmed:
                        n_unconfirmed += 1
                        logger.warning(
                            "撤单未确认终态 oid=%s（主推延迟或柜台未响应，需人工复核）", oid)
            except Exception:
                # 单笔失败不中断：记录后继续撤下一笔，最终返回成功发起数。
                logger.exception("熔断撤单失败 oid=%s", oid)
    logger.warning(
        "熔断撤单完成（内存回退路径），共发起撤 %s 笔未终态单（其中未确认 %s 笔）",
        n_cancelled, n_unconfirmed)
    return {"cancelled": n_cancelled, "unconfirmed": n_unconfirmed}
