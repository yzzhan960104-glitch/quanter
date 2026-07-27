# -*- coding: utf-8 -*-
"""板块只读端点（读内存湖，零写入）。

定位：前端驾驶舱「板块轮动」的后端供给。原宏观 CTA 端点 ``/macro/regime`` 与
``/macro/credit``（CreditRegime 信贷状态机）已于 2026-07 随 CreditRegime 整体
下线删除；``/macro/factors/{symbol}``（单标的 ATR 微观定权）于 T7 架构治理
批 2 删除——前端零调用方，颈线法自带 ``compute_atr`` 不依赖 factors 包。
数据管道（macro_credit 湖 + data/tools/sync_macro_credit.py）保留，不受影响。

端点清单：
    - GET /api/v1/macro/sector/flow        板块资金流排名 + 活跃股池

离线降级红线（贯穿全部端点）：
    开发机/CI 无数据湖（parquet 缺失、lifespan 未 load 任何湖）时，端点必须返
    【空结构】而非抛 500，让前端能渲染空图表容错；任何 raise 都会导致前端
    整页白屏。故每条端点顶部均做「无湖/空 df → 返空」短路返回。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from data.lake_reader import DataLakeReader

router = APIRouter(prefix="/macro", tags=["板块"])


# --------------------------------------------------------------
# 端点：/macro/sector/flow —— 板块资金流排名 + 活跃股池
# --------------------------------------------------------------

@router.get("/sector/flow", summary="板块资金流排名 + 活跃股池")
async def sector_flow() -> dict[str, Any]:
    """返回板块资金流 Top N 排名与活跃股池。

    响应结构：
        {
          "sectors": [板块记录 dict, ...],  # head(20) 板块资金流排名
          "pool":     [活跃股代码, ...]       # 活跃股池
        }

    离线降级：sector 湖未载入 → 返 {sectors: [], pool: []} 不抛。

    Why head(20)：前端面板仅展示 Top 20 板块，避免一次性渲染过多行拖慢交互。
    退役说明（2026-07-27）：原 sector 湖（akshare 板块资金流，sync_sector_daily 写）+
    a_shares_active 活跃池湖已整体退役——sectors 恒空（无数据源），pool 改走 daily 湖
    前 50 只（reader.symbols）。端点保留契约不崩前端，彻底下线待前端确认后移除。
    """
    import os as _os
    import pandas as _pd
    from config import LAKE_CONFIG
    # sector 湖已退役，LAKE_CONFIG 无此 key，.get 防 KeyError → sectors 恒空（无数据源）。
    sectors: list = []
    _sp = LAKE_CONFIG["lakes"].get("sector", "")
    if _sp and _os.path.exists(_sp):
        _sdf = _pd.read_parquet(_sp)
        if _sdf is not None and not _sdf.empty:
            sectors = _sdf.head(20).to_dict("records")
    # #5：活跃股池改走内存湖 reader.symbols()，杜绝每请求重读 408MB daily parquet。
    # 原实现每次请求 read_parquet(daily) 仅为取 symbol 列表，是 dashboard 性能黑洞；
    # reader.symbols() 走内存湖 index 零 IO。CI 无 daily 湖时返 [] → pool 空（离线降级）。
    reader = DataLakeReader.get_instance()
    syms = reader.symbols(lake="daily")[:50]
    pool = [{"symbol": str(s)} for s in syms]
    return {"sectors": sectors, "pool": pool}
