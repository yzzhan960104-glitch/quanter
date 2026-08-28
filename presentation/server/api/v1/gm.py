# -*- coding: utf-8 -*-
"""掘金终端（7002 网关）只读观测代理（W4-B，2026-08-28 全库评审清偿）。

物理定位：
    QMT 退役 P3 删除了 /api/v1/trading/* 路由与 QMT 网关，cockpit 三卡（心跳/资金/
    成交流水）与 /jobs 视图失去数据源——前端轮询死路由 404+toast 轰炸。本路由把
    掘金终端 7002 REST API（Bearer=策略目录 runtime.json token，skill=
    .agents/skills/goldminer-terminal 实测面）以【只读代理】形态接回 server，
    前端零凭证（token 只活在 server 进程，绝不入前端 bundle——client.ts 的
    VITE_API_TOKEN 内网假设不扩散到终端 token）。

边界红线：
    - 只代理 GET（持仓/资金/委托/成交流水/策略列表/账户状态）；下单/撤单/启停/
      风控配置族是 skill 纪律禁区（须用户显式指令），绝不进本路由。
    - 单源纪律：7002 调用与 runtime.json 读取全部经 ops.gm_ops_common（与三件套
      同源），本文件只做 HTTP 形态适配。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ops import gm_ops_common as gc

# 鉴权在 main.py 挂载层统一施加（require_read_cookie，同 logs_router 范式）——
# 本模块不自带依赖，保持与其它 v1 路由一致的"挂载点单源鉴权"结构。
router = APIRouter(prefix="/gm", tags=["gm-readonly"])


def _proxy(path: str) -> JSONResponse:
    """GET 7002 → 透传 JSON。非 200/连接失败 → 502 带中文排障信息（不裸抛）。"""
    try:
        cfg = gc.runtime_config()
    except Exception as e:                      # 缺 runtime.json/缺 token：策略目录异变
        raise HTTPException(502, f"掘金 runtime.json 不可读：{e}") from e
    token = str(cfg.get("token") or "")
    account = str(cfg.get("account_id") or "")
    status, payload = gc.api_get(path.format(account=account), token, timeout=4.0)
    if status != 200 or payload is None:
        return JSONResponse(status_code=502, content={
            "detail": f"掘金 7002 网关不可用（status={status}，path={path}）——"
                      "终端未开/网关断连，处置见 goldminer-terminal skill"})
    return JSONResponse(payload)


@router.get("/overview", summary="终端总览：策略列表 + 账户连接状态")
async def overview() -> JSONResponse:
    """StatusCard 数据源：策略 stage/进程态 + 账户通道连接（两次 7002 聚合）。"""
    try:
        cfg = gc.runtime_config()
        token = str(cfg.get("token") or "")
    except Exception as e:
        raise HTTPException(502, f"掘金 runtime.json 不可读：{e}") from e
    s1, strategies = gc.api_get("/v3/strategies", token, timeout=4.0)
    s2, accounts = gc.api_get("/v3/account-statuses", token, timeout=4.0)
    if s1 != 200:
        return JSONResponse(status_code=502, content={
            "detail": f"掘金 7002 网关不可用（status={s1}）——终端未开/网关断连"})
    return JSONResponse({"strategies": (strategies or {}).get("data") or [],
                         "account_statuses": (accounts or {}).get("data") or []
                         if s2 == 200 else None})


@router.get("/asset", summary="资金：nav/available/frozen/market_value")
async def asset() -> JSONResponse:
    return _proxy("/v3/account-trade/cash/{account}")


@router.get("/positions", summary="持仓：volume/vwap/fpnl/available")
async def positions() -> JSONResponse:
    return _proxy("/v3/account-trade/positions/{account}")


@router.get("/orders", summary="当日委托（含状态/拒因）")
async def orders() -> JSONResponse:
    return _proxy("/v3/account-trade/orders/{account}")


@router.get("/trades", summary="成交流水（execrpts：成交价量）")
async def trades() -> JSONResponse:
    return _proxy("/v3/account-trade/execrpts/{account}")
