# -*- coding: utf-8 -*-
"""层级二·因子注册表路由（薄封装 factor_service）。

端点：
- GET /api/v1/factors/registry            列出全部因子摘要（前端因子矩阵数据源）
- GET /api/v1/factors/{name}              单因子 drill-down（元数据 + 数据集 + 引用策略）
- GET /api/v1/factors/{name}/ic_decay     IC/IR 衰减曲线 + 月度×horizon 热力图

设计原则（与 strategies 路由同纪律）：
- 路由层从 app.state 取启动期扫描的 FactorLoader（+ StrategyLoader，用于反向查引用策略），
  不重复扫描；业务逻辑全在 factor_service。
- ic_decay 是 CPU 密集（面板构建 + 多 horizon IC），走 run_in_threadpool 避免阻塞事件循环。
- 未注册因子 → 404；非面板因子请求 ic_decay → 200 + ok=False + reason（前端友好降级，非错误）。
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from server.schemas.factor import (
    FactorSummary, FactorDetail, ICDecayResult,
)
from server.services import factor_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/factors", tags=["因子注册表"])


def _get_factor_loader(request: Request):
    """从 app.state 取启动期扫描的 FactorLoader（缺失则 500 暴露初始化问题）。"""
    loader = getattr(request.app.state, "factor_loader", None)
    if loader is None:
        raise HTTPException(status_code=500, detail="因子加载器未初始化")
    return loader


def _get_strategy_loader(request: Request):
    """取 StrategyLoader（可能缺失，drill-down 引用策略据此降级为空列表）。"""
    return getattr(request.app.state, "strategy_loader", None)


@router.get("/registry", response_model=List[FactorSummary], summary="列出全部因子摘要")
async def list_factors(request: Request) -> List[FactorSummary]:
    """反射 FactorLoader 全局注册表（前端因子矩阵按 status/category 分类展示）。"""
    return factor_service.list_factors(_get_factor_loader(request))


@router.get("/{name}", response_model=FactorDetail, summary="单因子 drill-down")
async def get_factor(request: Request, name: str) -> FactorDetail:
    """返回单因子元数据 + 关联数据集 + 引用策略（drill-down 头部）。"""
    detail = factor_service.get_detail(
        _get_factor_loader(request), _get_strategy_loader(request), name
    )
    if detail is None:
        raise HTTPException(status_code=404, detail=f"未注册的因子: {name}")
    return FactorDetail(**detail)


@router.get("/{name}/ic_decay", response_model=ICDecayResult, summary="IC/IR 衰减分析")
async def get_ic_decay(
    request: Request,
    name: str,
    start: str = Query(..., description="评估区间起 'YYYY-MM-DD'"),
    end: str = Query(..., description="评估区间止 'YYYY-MM-DD'"),
    universe: Optional[List[str]] = Query(None, description="标的列表；缺省走活跃池"),
    horizons: Optional[List[int]] = Query(None, description="持有期；缺省 [1,3,5,10,20]"),
) -> ICDecayResult:
    """IC/IR 衰减曲线 + 月度×horizon 热力图（仅面板型因子支持）。

    非面板因子 → 200 + ok=False + reason（前端展示「不支持 IC 衰减」而非报错）。
    CPU 密集 → run_in_threadpool 避免阻塞 ASGI 事件循环。
    """
    result = await run_in_threadpool(
        factor_service.compute_ic_decay,
        _get_factor_loader(request), name, start, end, universe, horizons,
    )
    return ICDecayResult(**result)
