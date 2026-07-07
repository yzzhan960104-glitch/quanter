# -*- coding: utf-8 -*-
"""实盘交易路由：薄封装 trading_service，阻塞调用走 run_in_threadpool。

端点：
- GET  /api/v1/trading/status        心跳四态（前端轮询镜像）
- GET  /api/v1/trading/positions     底层持仓聚合（Treemap 数据源）
- POST /api/v1/trading/emergency_halt 一键熔断（幂等）

异常策略：
- trading_service.emergency_halt/get_positions 在网关 unavailable 时 raise RuntimeError
  → 本层按消息关键字分流：未连接/锁定 → 409；未装配/unavailable → 503；其余 → 500。

Why emergency_halt 投线程池：它是同步函数（含 setattr + 日志 + fire_and_forget），
虽无重 CPU，但保持与既有 portfolio 路由同纪律（同步业务逻辑统一 run_in_threadpool），
避免在事件循环里直接执行潜在的阻塞日志 IO。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from pydantic import BaseModel
from trading.execution_gateway import OrderRequest
from server.services.trading_service import (
    emergency_halt,
    export_trades,
    get_positions,
    get_status,
    connect_gateway,
    disconnect_gateway,
    submit_order as svc_submit_order,
    cancel_order as svc_cancel_order,
    get_orders,
    get_asset,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/trading", tags=["实盘交易"])


@router.get("/status", summary="网关心跳四态")
async def status() -> dict:
    """前端 Cockpit 每 2s 轮询；严格镜像后端状态机。"""
    return get_status()


@router.get("/positions", summary="底层真实持仓聚合")
async def positions() -> dict:
    """Treemap 数据源。未连接/锁定 → 409；网关未装配 → 503。"""
    try:
        rows = await get_positions()
        return {"positions": rows}
    except RuntimeError as e:
        msg = str(e)
        if "未连接" in msg or "锁定" in msg:
            raise HTTPException(409, msg)
        if "未装配" in msg or "unavailable" in msg:
            raise HTTPException(503, msg)
        raise HTTPException(500, msg)


@router.post("/emergency_halt", summary="一键熔断（幂等）")
async def halt() -> dict:
    """红色大按钮后端。幂等：重复调用不再重复处理。"""
    try:
        return await run_in_threadpool(emergency_halt)
    except RuntimeError as e:
        # 网关未装配 → 503
        raise HTTPException(503, str(e))


@router.get("/export", summary="导出实盘成交 CSV（按日期）")
async def export_live_trades(
    start: str = Query(..., description="起 'YYYY-MM-DD'"),
    end: str = Query(..., description="止 'YYYY-MM-DD'"),
) -> Response:
    """导出 [start,end] 区间实盘成交日志（logs/live_trades.csv）为标准 CSV。

    无日志 → 仅表头（诚实空导出，非 404）。Layer 6 LLM 复盘直接消费此 CSV。
    """
    csv_str = await run_in_threadpool(export_trades, start, end)
    return Response(
        content=csv_str,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="live_trades_{start}_{end}.csv"',
        },
    )


# ============================================================================
# Phase 1 Task 6：连接 / 下单 / 撤单 / 查询路由
# ============================================================================
class SubmitOrderBody(BaseModel):
    """下单请求体。dry_run 默认 True（安全缺省模拟）；confirm 默认 False（强制二次确认）。"""
    symbol: str
    qty: float
    side: str                       # "buy" / "sell"
    price: float | None = None      # None=市价；有值=限价
    dry_run: bool = True            # 前端控制：True=模拟（不真下单）
    confirm: bool = False           # 二次确认开关


@router.post("/connect", summary="触发 QMT 网关连接")
async def connect_gw() -> dict:
    """连接 MiniQMT。失败 → 503（客户端未启动登录/路径错）。"""
    try:
        await connect_gateway()
        return {"connected": True, "mode": "live"}
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except ConnectionError as e:
        raise HTTPException(503, str(e))


@router.post("/disconnect", summary="断开 QMT 网关")
async def disconnect_gw() -> dict:
    """优雅断开网关。无网关时静默返 connected=False（不报错）。"""
    await disconnect_gateway()
    return {"connected": False}


@router.post("/submit_order", summary="下单（dry_run 前端可控）")
async def submit_order_endpoint(body: SubmitOrderBody) -> dict:
    """下单：dry_run=true 模拟（落 DRY_RUN 流水）；挡板命中 → 409；全过 → 真下单。

    交易流水全覆盖（spec §6.3）：dry_run / BLOCKED / 真单 / 废单 / 撤单 均落 CSV。
    """
    order = OrderRequest(symbol=body.symbol, qty=body.qty, side=body.side, price=body.price)
    try:
        return await svc_submit_order(order, dry_run=body.dry_run, confirm=body.confirm)
    except RuntimeError as e:
        # 挡板命中（非 dry_run）→ 409
        raise HTTPException(409, str(e))


@router.post("/cancel_order/{order_id}", summary="撤单")
async def cancel_order_endpoint(order_id: str) -> dict:
    """撤单：透传网关。seq→real 映射未就绪 → state=FAILED（引导短暂重试）。"""
    try:
        return await svc_cancel_order(order_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@router.get("/orders", summary="本地订单回报流水")
async def orders_endpoint() -> dict:
    """查询网关缓存的订单回报。无网关 → 空 list（200，非 503）。"""
    return {"orders": await get_orders()}


@router.get("/asset", summary="资金资产")
async def asset_endpoint() -> dict:
    """查询资金资产（现金/总资产/市值）。无网关或未连接 → 空 dict（200）。"""
    return {"asset": await get_asset()}
