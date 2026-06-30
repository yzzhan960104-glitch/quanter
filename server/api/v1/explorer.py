"""因子探索沙盒路由：CPU 探针拒绝 + Celery 派发 + Redis 宕机降级。

风控红线（Why）：
- CPU 探针：因子网格是 CPU 密集型任务，若宿主机已高负载（>80%），盲目派发会与
  实时交易/回测线程抢核，可能拖垮下单时延。故先探针，超阈值直接 429 拒绝，
  让调用方退避重试，绝不压垮宿主机。
- Redis 宕机降级：Redis 不可用时绝不能阻断 API（否则一个 Redis 抖动就导致整个
  因子沙盒不可用）。捕获 redis.ConnectionError → 钉钉告警（fire_and_forget）→
  降级到 starlette 线程池同步执行，返回 degraded=True 让前端感知降级态。
"""
from __future__ import annotations

import logging

import psutil
import redis
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from config import CELERY_CONFIG
from core.notifier import NotificationManager, fire_and_forget
from server.celery_app import run_factor_grid, run_factor_grid_impl

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/explorer", tags=["因子沙盒"])


class FactorGridSpec(BaseModel):
    """因子网格计算规格。

    factor: 因子名（与 factors 模块暴露的函数名对齐，如 cross_sectional_momentum）；
    universe: 标的列表（A 股代码，如 000001.SZ）；
    start/end: 评估区间（ISO 日期字符串，由 DataLakeReader 解析）。
    """
    factor: str
    universe: list[str]
    start: str
    end: str


@router.post("/grid", summary="提交因子网格计算")
async def submit_grid(spec: FactorGridSpec):
    """CPU > 阈值拒绝；Redis 宕机降级线程池。

    返回：
    - 正常：{task_id, degraded: False}（Celery 异步派发，前端轮询 /result）；
    - 降级：{result, degraded: True}（线程池同步执行完，结果直接返回）。
    """
    # CPU 探针：interval=0.1 取 100ms 采样，兼顾响应性与准确性；
    # 超阈值直接 429，让前端按业务策略退避重试，避免压垮宿主机。
    if psutil.cpu_percent(interval=0.1) > CELERY_CONFIG["cpu_gate_percent"]:
        raise HTTPException(429, "CPU 负载过高，拒绝调度")
    try:
        task = run_factor_grid.delay(spec.model_dump())
        return {"task_id": task.id, "degraded": False}
    except redis.ConnectionError:
        # 风控红线：Redis 不可用 → 钉钉告警 + 降级线程池，绝不阻断
        # fire_and_forget 起独立 daemon 线程跑协程，告警失败仅记日志，
        # 不影响本请求的降级执行路径（告警与降级解耦）。
        fire_and_forget(NotificationManager.get_default().notify_risk_event(
            "Redis 不可用，因子网格降级到线程池执行", "WARN"))
        logger.warning("Redis 不可用，explorer 降级线程池")
        # run_in_threadpool 把同步 impl 放进 starlette 线程池执行，
        # 避免阻塞 ASGI 事件循环（因子计算为 CPU 密集，直接在事件循环里跑会卡死其它请求）。
        result = await run_in_threadpool(run_factor_grid_impl, spec.model_dump())
        return {"result": result, "degraded": True}


@router.get("/result/{task_id}", summary="查询因子网格结果")
async def get_result(task_id: str):
    """查 Celery AsyncResult 状态。

    status 取 Celery 标准状态串（PENDING/STARTED/SUCCESS/FAILURE/RETRY）；
    ready=True 时 result 已就绪（成功为返回值，失败为异常对象）。
    """
    from celery.result import AsyncResult
    from server.celery_app import celery_app
    res = AsyncResult(task_id, app=celery_app)
    return {"status": res.status, "ready": res.ready(),
            "result": res.result if res.ready() else None}
