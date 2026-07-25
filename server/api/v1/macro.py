# -*- coding: utf-8 -*-
"""板块/因子只读端点（读内存湖，零写入）。

定位：前端驾驶舱「板块轮动 + 微观定权」的后端供给。原宏观 CTA 端点
``/macro/regime`` 与 ``/macro/credit``（CreditRegime 信贷状态机）已于 2026-07
随 CreditRegime 整体下线删除——宏观逻辑后续重建时再恢复；数据管道
（macro_credit 湖 + data/tools/sync_macro_credit.py）保留，不受影响。

端点清单：
    - GET /api/v1/macro/sector/flow        板块资金流排名 + 活跃股池
    - GET /api/v1/macro/factors/{symbol}   单标的 ATR 波动率（微观定权）

离线降级红线（贯穿全部端点）：
    开发机/CI 无数据湖（parquet 缺失、lifespan 未 load 任何湖）时，端点必须返
    【空结构】而非抛 500，让前端能渲染空图表容错；任何 raise 都会导致前端
    整页白屏。故每条端点顶部均做「无湖/空 df → 返空」短路返回。

无前视红线：
    ATR 读取历史窗口亦只取过去值（_shift(30) → _today()，不含未来日）。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

import pandas as pd
from fastapi import APIRouter

from data.lake_reader import DataLakeReader

router = APIRouter(prefix="/macro", tags=["板块/因子"])


# --------------------------------------------------------------
# 辅助：日期工具（端点内联，避免污染 config 层）
# --------------------------------------------------------------

def _today() -> str:
    """今日日期（YYYY-MM-DD 字符串）。

    Why 字符串：DataLakeReader 内部 _norm_date 会按湖 date 层级原生 dtype
    归一化查询键，端点侧统一传字符串最稳妥（兼容 str/datetime 两类索引）。
    """
    return _dt.date.today().strftime("%Y-%m-%d")


def _shift(days: int) -> str:
    """今日往前推 days 日的日期字符串。

    用途：/macro/factors ATR 读取 [today-30, today] 窗口的分钟时序，
    30 日窗口保证 ATR 滚动均值有足够样本（14 bar 默认窗口 + 余量）。
    """
    return (_dt.date.today() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")


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
    Why pool 暂返 []：活跃股池目前由 sync_sector_daily 单独落盘到独立结构，
    本期端点先留字段占位（前端契约已对齐），下期接入活跃股池湖后填充。
    """
    import os as _os
    import pandas as _pd
    from config import LAKE_CONFIG
    # sector 资金流是【快照排名表】（RangeIndex，非时序），DataLakeReader 只载时序湖会跳过它，
    # 故此处直读 parquet（小表 ~500 行，IO 可忽略）。
    sectors: list = []
    _sp = LAKE_CONFIG["lakes"]["sector"]
    if _os.path.exists(_sp):
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


# --------------------------------------------------------------
# 端点：/macro/factors/{symbol} —— 单标的 ATR 波动率
# --------------------------------------------------------------

@router.get("/factors/{symbol}", summary="单标的 ATR 波动率")
async def factors(symbol: str) -> dict[str, Any]:
    """返回单标的近 30 日窗口的 ATR 波动率（微观 Risk Parity 定权用）。

    响应结构：
        {"atr": float | None}   # ATR 值，时序空时返 None

    离线降级：minute 湖未载入或 symbol 无时序 → get_timeseries 返空 df → 返
    {atr: None} 不抛（前端显示「无数据」而非崩溃）。

    Why get_timeseries(symbol, ...) 而非直读 _lakes：minute 湖是 MultiIndex
    (date, symbol)，标准 xs 查询路径正确；与 sector 端点不同。
    Why 30 日窗口：ATR 默认 14 bar 滚动窗口，30 日保证有足够样本算出末值，
    又不会因窗口过长引入过时波动率。
    """
    # 延迟 import：仅此端点用 ATR，避免顶层 import 污染 sector 端点的导入图。
    # ATR 已从 factors/micro_momentum 迁到 core/indicator（Phase 1·Task 3 因子体系剥离）。
    from factors.atr import atr

    ts = DataLakeReader.get_instance().get_timeseries(
        symbol, _shift(30), _today(), lake="minute"
    )
    # 离线降级：时序空（无湖/symbol 不存在/窗口无数据）→ 返 None
    if ts.empty:
        return {"atr": None}
    # ATR 末值：micro_momentum.atr 返 Series，取 .iloc[-1] 即最新一日 ATR
    v = atr(ts).iloc[-1]
    # ★ NaN/不足窗口守卫（双保险）：
    #   1) 窗口不足守卫：atr() 默认 14 bar 滚动窗口，当 minute 湖 bar 数 < 14 时
    #      rolling 末值本应为 NaN——但 micro_momentum.atr 内部 .where(a>1e-9, 1e-9)
    #      会把 NaN 一并替换成 1e-9（防除零 ε），即「窗口不足」被静默伪装成 1e-9 的
    #      伪 ATR。这比裸 NaN 更危险：前端会信以为真地画出错误的微观波动率定权。
    #      故必须在端点侧显式按序列长度判窗口，bar 数不足 → 返 None（语义=数据不足）。
    #   2) NaN 直通守卫：即便未来 atr() 实现变更不再以 ε 兜底，pd.isna(v) 仍能把
    #      裸 NaN 降级为 None——float(NaN) 会让 FastAPI 默认 json 编码器发出字面
    #      "NaN" token（非法 JSON，JS JSON.parse/前端 axios 抛 SyntaxError 致整页
    #      白屏），违背本文件「绝不致前端白屏」降级红线。两层守卫缺一不可。
    if len(ts) < 14 or pd.isna(v):
        return {"atr": None}
    return {"atr": float(v)}
