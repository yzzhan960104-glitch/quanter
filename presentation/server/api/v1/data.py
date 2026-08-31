# -*- coding: utf-8 -*-
"""层级一·数据湖资产路由（薄封装 data_service）。

端点：
- GET  /api/v1/data/datasets        列出全部数据集资产（前端表格数据源）

设计原则（与 strategies/trading 路由同纪律）：
- 路由层只做参数校验 + 调 service + 异常转 HTTP；业务逻辑全在 data_service。
- list_datasets 纯读文件系统 + 内存湖，无阻塞 IO，直接同步返回（不走 run_in_threadpool）。
- 使用 response_model 暴露 Pydantic 契约（OpenAPI 可见，前端类型对齐有据）。

历史：POST /sync/{key} 已随 2026-08-31 写端点全量退役删除——手动触发入口早在
DataLakeView 撤「立即同步」时已无消费者，同步统一由 lifespan 启动 sweep 与
18:00 pipeline 事件链编排（data_service.trigger_sync 保留，非 HTTP 驱动）。
"""
import logging
from typing import List

from fastapi import APIRouter

from presentation.server.schemas.data import DatasetAsset
from presentation.server.services import data_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/data", tags=["数据湖"])


@router.get("/datasets", response_model=List[DatasetAsset], summary="列出全部数据集资产")
async def list_datasets() -> List[DatasetAsset]:
    """反射 DATASET_REGISTRY + parquet mtime/哨兵 派生状态。

    每条字段：key/name/source/market/granularity/schedule/status/
    data_start/data_end/latest_sync/last_error（前端 DataLakeView 表格直接消费）。
    """
    return data_service.list_datasets()
