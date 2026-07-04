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

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from server.services.trading_service import (
    emergency_halt,
    get_positions,
    get_status,
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
