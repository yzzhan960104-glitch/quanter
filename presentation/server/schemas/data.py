# -*- coding: utf-8 -*-
"""层级一·数据湖资产的 Pydantic 契约。

- DatasetAsset：单条数据集资产（GET /data/datasets 返回项，前端表格直接消费）。
- SyncResponse：触发同步的响应（POST /data/sync/{key}）。

设计原则：
- 字段全部 JSON 可序列化；时间用 ISO 字符串，缺失用 None（前端容错展示 '—'）。
- status 枚举与 data_service._derive_status 的状态机严格同源，前端徽章按此着色。
"""
from typing import List, Optional, Literal

from pydantic import BaseModel

# 数据集状态五态（前端按此枚举着色；与 data_service._derive_status 同源，单一真相源）
DatasetStatus = Literal["syncing", "healthy", "stale", "missing", "failed"]


class DatasetAsset(BaseModel):
    """单条数据集资产（反射 DATASET_REGISTRY + parquet mtime/哨兵 派生态）。"""
    key: str                            # 湖 key（与 LAKE_CONFIG["lakes"] 同源，主键）
    name: str                           # 中文展示名（表格首列）
    source: str                         # 数据源（AKShare/JQData/Binance/...）
    market: str                         # 市场口径（A股/美股/加密/宏观/板块）
    granularity: str                    # 粒度（1m/1d/月频）
    schedule: str                       # 计划节奏（仅元信息，无 Beat 强约束）
    status: DatasetStatus               # 当前状态（mtime + 哨兵联合推导）
    data_start: Optional[str] = None    # 数据起始日（ISO；内存湖未载入则 None）
    data_end: Optional[str] = None      # 最新数据日（ISO；同上）
    latest_sync: Optional[str] = None   # 最近一次同步时刻（parquet mtime ISO；缺失则 None）
    last_error: Optional[str] = None    # 失败原因（status=failed 时填，stderr 尾部）


class SyncResponse(BaseModel):
    """POST /data/sync/{key} 响应。"""
    key: str
    status: DatasetStatus               # 触发后恒为 syncing（key 非法由路由层转 404）
    message: str


class DatasetListResponse(BaseModel):
    """GET /data/datasets 响应包装（便于未来追加汇总统计字段）。"""
    items: List[DatasetAsset]
    total: int
