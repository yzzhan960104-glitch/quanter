# -*- coding: utf-8 -*-
"""另类因子（北向资金 / 龙虎榜情绪）。

数据源：
- data_lake/north_flow.parquet（DatetimeIndex 单序列，north_net_flow 亿元）
- data_lake/dragon_list.parquet（MultiIndex(date, symbol)，龙虎榜上榜记录）

设计原则：
- 纯读 DataLakeReader 内存湖，不取数（启动时 lifespan 已 load）。
- 只产出原始信号（累计净流入 / 上榜集合），方向/阈值/组合由策略层决定，因子层不预设偏好。
- 湖未加载时静默返空（离线降级，不抛）。
"""
from __future__ import annotations

import logging
from typing import Set

import pandas as pd

from data.lake_reader import DataLakeReader

logger = logging.getLogger(__name__)


def north_flow_momentum(start: str, end: str, *, window: int = 5) -> pd.Series:
    """北向资金连续净流入动量：近 window 日累计净流入（亿元）。

    参数：
        start/end: 'YYYY-MM-DD' 区间。
        window: 滚动窗口（默认 5 个交易日，约一周）。

    返回：
        DatetimeIndex × 累计净流入。正=持续流入（外资看多领先），负=持续流出。
        湖未加载/无数据返空 Series。
    """
    reader = DataLakeReader.get_instance()
    lake = reader._lakes.get("north_flow")
    if lake is None or lake.empty or "north_net_flow" not in lake.columns:
        logger.debug("north_flow 湖未加载，north_flow_momentum 返空")
        return pd.Series(dtype=float)
    series = lake["north_net_flow"].loc[start:end]
    if series.empty:
        return series
    return series.rolling(window).sum().dropna()


def dragon_signal(date: str) -> Set[str]:
    """龙虎榜当日上榜 symbol 集合。

    参数：
        date: 'YYYY-MM-DD'。

    返回：
        {symbol, ...} 当日上榜个股集合（供策略层做关注度/情绪过滤）。
        湖未加载/当日无上榜/日期不存在返空 set。
    """
    reader = DataLakeReader.get_instance()
    lake = reader._lakes.get("dragon_list")
    if lake is None or lake.empty:
        return set()
    # 直接从 MultiIndex 过滤（避免 xs 在无列 DataFrame 上的边界行为，更稳健）
    idx = lake.index
    if "date" not in idx.names or "symbol" not in idx.names:
        return set()
    ts = pd.Timestamp(date).normalize()
    mask = idx.get_level_values("date") == ts
    return set(idx.get_level_values("symbol")[mask])
