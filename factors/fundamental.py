# -*- coding: utf-8 -*-
"""基本面因子（横截面估值 / 质量分位）。

数据源：data_lake/fundamentals.parquet（sync_fundamentals.py 落盘的 daily_basic 估值面板），
MultiIndex(date, symbol) × [pe,pb,ps,pe_ttm,dv_ratio,total_mv,circ_mv]。

设计原则（极简 + 显式）：
- 不重新取数：纯读 DataLakeReader 的 fundamentals 湖（启动时 lifespan 已 load）。
- 横截面分位：截面当日全市场排序打分（0~1），跨标的可比；不做时序计算（留给策略层）。
- 方向参数：pe/pb 高可能是高估（负向因子）也可能是成长股溢价（正向），由 direction 决定，
  避免在因子层硬编码价值/成长偏好。

NaN 防线：截面 rank 默认 na_option="keep"，NaN 不参与排序（不污染分位），上游 _safe_float 兜底。
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from data.lake_reader import DataLakeReader
from .base import register_factor, FactorMeta

logger = logging.getLogger(__name__)


@register_factor(FactorMeta(
    name="valuation_cross_section",
    label="横截面估值",
    category="估值",
    status="training",
    input_kind="cross_section",     # 逐日截面，非时序面板，不参与标准 IC 网格
    dataset="fundamentals",
    description="全市场当日估值分位（0~1，pe_ttm/pb/dv_ratio 等）。方向由 direction 决定（价值/成长）。",
    default_params={"field": "pe_ttm", "direction": "value"},
))
def valuation_cross_section(
    date: str,
    field: str = "pe_ttm",
    *,
    direction: str = "value",
    lake_key: str = "fundamentals",
) -> pd.Series:
    """横截面估值分位（0~1），全市场当日排序。

    参数：
        date: 'YYYY-MM-DD' 截面日。
        field: 估值字段（pe/pe_ttm/pb/pb_ttm/ps/ps_ttm/dv_ratio/total_mv/circ_mv）。
        direction: 'value'（价值，低估值高分）或 'growth'（成长，高估值高分）。
            Why 显式方向：pe 高可能是高估（价值派应低分）也可能是成长溢价（成长派应高分），
            因子层不预设偏好，由调用方按策略意图决定。
        lake_key: 基本面湖 key（默认 'fundamentals'）。

    返回：
        pd.Series，index=symbol，values=分位（0~1，越高越"好"按 direction 语义）。
        无该日数据返空 Series。
    """
    reader = DataLakeReader.get_instance()
    panel = reader.get_cross_section(date, lake=lake_key)
    if panel is None or panel.empty or field not in panel.columns:
        logger.debug("fundamentals 湖 %s 无 %s 列/数据", date, field)
        return pd.Series(dtype=float)

    series = panel[field]
    # rank：截面排序，pct=True 得 0~1 分位；na_option=keep 保留 NaN（不污染分位）
    rank = series.rank(pct=True, na_option="keep")
    if direction == "value":
        # 价值派：低估值（field 值小）→ 高分（rank 反转）
        return (1.0 - rank).dropna()
    # 成长派：高估值（field 值大）→ 高分
    return rank.dropna()


def size_factor(date: str, *, lake_key: str = "fundamentals") -> pd.Series:
    """市值因子（log 总市值），用于中性化或规模分桶。

    返回 pd.Series(index=symbol, values=log(total_mv))。规模效应（小盘溢价）是经典因子，
    本函数只产出原始 log 市值，中性化/分桶由上游决定。
    """
    reader = DataLakeReader.get_instance()
    panel = reader.get_cross_section(date, lake=lake_key)
    if panel is None or panel.empty or "total_mv" not in panel.columns:
        return pd.Series(dtype=float)
    import numpy as np
    return np.log(panel["total_mv"].clip(lower=1e-6)).dropna()


def get_fundamentals_timeseries(
    symbol: str,
    start: str,
    end: str,
    *,
    field: Optional[str] = None,
    lake_key: str = "fundamentals",
) -> pd.DataFrame:
    """单标的基本面时序（透传 reader.get_timeseries）。

    供策略层取某标的的历史估值（如 PE 历史分位择时）。
    """
    reader = DataLakeReader.get_instance()
    df = reader.get_timeseries(symbol, start, end, lake=lake_key)
    if df is None or df.empty:
        return pd.DataFrame()
    return df[[field]] if field and field in df.columns else df
