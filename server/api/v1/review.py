# -*- coding: utf-8 -*-
"""层级六·AI 复盘路由（薄封装 review_service）。

端点：
- POST /api/v1/review/diagnose   组装 Prompt + 调 GLM → Markdown 复盘报告

设计原则：
- LLM 调用是阻塞网络 IO，走 run_in_threadpool 避免阻塞 ASGI 事件循环。
- review_service 内部三级降级（缺凭证/调用失败/无数据），本层不重复降级逻辑；
  始终返 200 + ReviewReport（degraded 字段让前端感知降级态），非输入错误不抛 5xx。
"""
import logging

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from server.schemas.review import ReviewRequest, ReviewReport
from server.services import review_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/review", tags=["AI 复盘"])


@router.post("/diagnose", response_model=ReviewReport, summary="AI 复盘诊断（GLM）")
async def diagnose(req: ReviewRequest) -> ReviewReport:
    """组装实盘日志 + 策略上下文 → GLM → Markdown 复盘报告。

    - 数据源：csv_text（上传）或 start/end（读 logs/live_trades.csv）。
    - GLM_API_KEY 缺失/调用失败 → 降级返回上下文摘要（degraded=true），不阻断。
    - 超时 60s（LLM 推理耗时，前端请用长超时 + loading）。
    """
    return await run_in_threadpool(review_service.diagnose, req)
