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


async def cancel_all_open_orders(gw: Any) -> dict[str, int]:
    """撤销网关下所有未终态订单（熔断/断线/紧急停机时调）。

    参数：
        gw: 执行网关实例，需暴露 ``_orders: dict[str, dict]``（与
            QmtExecutionGateway 同口径）与 async ``cancel_order(order_id)``；
            可选暴露 async ``_confirm_cancelled(oid, timeout, interval) -> bool``
            （T2 产出，用于撤单后轮询确认到终态）。未挂该方法的网关（dry_run/
            老 Mock）走鸭子类型跳过确认，行为向后兼容。

    返回：
        ``{"cancelled": int, "unconfirmed": int}``
          - ``cancelled``：成功「发起」撤单的笔数（非「已撤成功」，柜台回报
            最终态有滞后）。
          - ``unconfirmed``：发起后 ``_confirm_cancelled`` 超时未确认到终态
            的笔数（主推延迟或柜台未响应）。``unconfirmed>0`` 仅告警，不阻塞
            熔断/挂单主路径——撤单已发，本地状态终会被 on_cancel_error/
            on_stock_order 对账修正；但必须显式暴露此口径，让调用方据以决定
            是否人工复核，杜绝「本地以为撤了、柜台其实没撤」的状态悬空。

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

    阻塞上界（调用方必须评估）：
        本函数对 N 笔未终态单【串行】调 cancel_order + _confirm_cancelled，
        每笔最坏 ``timeout=5s``（_confirm_cancelled 轮询超时），故整体最坏
        ``5N 秒``（N=未终态单数）。调用方需据 N 评估是否阻塞主路径：
          - ``pre_open`` 撤昨日未成交单 / 日内熔断撤单：N 通常个位数（昨夜遗留
            挂单 + 当日未成交买单），5N≈数十秒可接受，且都在非交易窗口或风控
            触发后执行，阻塞无副作用；
          - 若 N 可能上百（如批量挂单后紧急停机），调用方需自行评估是否放到
            后台 task 或缩短 timeout，避免阻塞 event loop 致行情/订单回调饿死。
    """
    orders = getattr(gw, "_orders", {}) or {}
    # 鸭子类型探针：T2 产出的确认方法（仅 QmtExecutionGateway 挂载）；
    # None 表示网关不支持确认（dry_run/老 Mock），退化为「不确认、unconfirmed 恒 0」。
    confirm_fn = getattr(gw, "_confirm_cancelled", None)
    n_cancelled = 0
    n_unconfirmed = 0
    for oid, rec in list(orders.items()):
        if rec.get("state") not in _TERMINAL:
            try:
                await gw.cancel_order(oid)
                n_cancelled += 1
                # M2 撤单确认闭环（T3 接入）：撤单「发起」成功后，轮询确认是否
                # 真到终态。QMT 主推延迟 1-2s，不确认则状态悬空（[[qmt-live-smoke-findings]]）。
                # True=到终态（CANCELLED 撤成 或 FILLED 撤晚已成，都表示不再 pending）；
                # False=超时未确认 → 计 unconfirmed + WARNING，不假装撤成。
                if confirm_fn is not None:
                    confirmed = True
                    try:
                        confirmed = await confirm_fn(
                            str(oid), timeout=5.0, interval=0.5
                        )
                    except Exception:
                        # 确认自身异常保守视为未确认（见 docstring Why）。
                        confirmed = False
                        logger.exception(
                            "撤单确认异常（保守计未确认）oid=%s", oid
                        )
                    if not confirmed:
                        n_unconfirmed += 1
                        logger.warning(
                            "撤单未确认终态 oid=%s（主推延迟或柜台未响应，需人工复核）",
                            oid,
                        )
            except Exception:
                # 单笔失败不中断：记录后继续撤下一笔，最终返回成功发起数。
                logger.exception("熔断撤单失败 oid=%s", oid)
    logger.warning(
        "熔断撤单完成，共发起撤 %s 笔未终态单（其中未确认 %s 笔）",
        n_cancelled,
        n_unconfirmed,
    )
    return {"cancelled": n_cancelled, "unconfirmed": n_unconfirmed}
