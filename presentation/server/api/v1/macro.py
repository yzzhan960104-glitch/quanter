# -*- coding: utf-8 -*-
"""活跃股池只读端点（读内存湖，零写入）。

定位：前端驾驶舱「活跃股池」的后端供给。

下线史（防复活注记）：
    - 原宏观 CTA 端点 ``/macro/regime`` 与 ``/macro/credit``（CreditRegime 信贷状态机）
      已于 2026-07 随 CreditRegime 整体下线删除；``/macro/factors/{symbol}``（单标的 ATR
      微观定权）于 T7 架构治理批 2 删除（前端零调用方）。
    - **``/macro/sector/flow``（板块资金流排名 + 活跃股池）已于 2026-08-15 CR-8 处置删除**
      （技术债波次 Task 16）：sector 湖 2026-07-27 退役后 sectors 字段结构性恒空，前端
      板块图块长期渲染空态水印——本波次前端确认下线，端点收缩为纯活跃股池供给，路由
      同步改名为 ``/macro/pool``（旧路径不再注册，契约 gate 保证前后端同步）。
    - 数据管道（macro_credit 湖 + data/tools/sync_macro_credit.py）不受影响，保留。

端点清单：
    - GET /api/v1/macro/pool          活跃股池（daily 内存湖前 50 只）

离线降级红线（贯穿全部端点）：
    开发机/CI 无数据湖（parquet 缺失、lifespan 未 load 任何湖）时，端点必须返
    【空结构】而非抛 500，让前端能渲染空图表容错；任何 raise 都会导致前端
    整页白屏。故端点顶部做「无湖 → 返空」短路返回。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from data.lake_reader import DataLakeReader

router = APIRouter(prefix="/macro", tags=["板块"])


# --------------------------------------------------------------
# 端点：/macro/pool —— 活跃股池
# --------------------------------------------------------------

@router.get("/pool", summary="活跃股池")
async def active_pool() -> dict[str, Any]:
    """返回活跃股池（daily 内存湖 symbol 序前 50 只）。

    响应结构：
        {
          "pool": [活跃股记录, ...]   # [{"symbol": "000001.SZ"}, ...]
        }

    离线降级：daily 湖未载入 → 返 {pool: []} 不抛（前端空表容错）。

    Why reader.symbols() 而非每请求 read_parquet（#5 性能红线）：活跃股池只需 symbol
    列表，走内存湖 index 零 IO；原实现每请求重读 408MB daily parquet 是 dashboard
    性能黑洞。CI 无 daily 湖时返 [] → pool 空（离线降级）。
    """
    reader = DataLakeReader.get_instance()
    syms = reader.symbols(lake="daily")[:50]
    pool = [{"symbol": str(s)} for s in syms]
    return {"pool": pool}
