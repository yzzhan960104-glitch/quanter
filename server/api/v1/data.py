# -*- coding: utf-8 -*-
"""层级一·数据湖资产路由（薄封装 data_service）。

端点：
- GET  /api/v1/data/datasets        列出全部数据集资产（前端表格数据源）
- POST /api/v1/data/sync/{key}      触发某数据集同步（写哨兵 + 后台子进程）

设计原则（与 strategies/trading 路由同纪律）：
- 路由层只做参数校验 + 调 service + 异常转 HTTP；业务逻辑全在 data_service。
- list_datasets 纯读文件系统 + 内存湖，无阻塞 IO，直接同步返回（不走 run_in_threadpool）。
- trigger_sync 仅写哨兵 + 起 daemon 线程（毫秒级），亦直接返回。
- 使用 response_model 暴露 Pydantic 契约（OpenAPI 可见，前端类型对齐有据）。
"""
import logging
from typing import List

from fastapi import APIRouter, HTTPException

from server.schemas.data import DatasetAsset, SyncResponse
from server.services import data_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/data", tags=["数据湖"])


@router.get("/datasets", response_model=List[DatasetAsset], summary="列出全部数据集资产")
async def list_datasets() -> List[DatasetAsset]:
    """反射 DATASET_REGISTRY + parquet mtime/哨兵 派生状态。

    每条字段：key/name/source/market/granularity/schedule/status/
    data_start/data_end/latest_sync/last_error（前端 DataLakeView 表格直接消费）。
    """
    return data_service.list_datasets()


@router.post("/sync/{key}", response_model=SyncResponse, summary="触发某数据集同步")
async def trigger_sync(key: str) -> SyncResponse:
    """写 .syncing/{key} 哨兵 + 后台 daemon 子进程跑 sync 脚本，立即返回 syncing。

    幂等：syncing 中重复触发直接返回 syncing，不二次派发（防 parquet 互覆盖）。
    key 未登记 → 404。
    """
    try:
        return SyncResponse(**data_service.trigger_sync(key))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
