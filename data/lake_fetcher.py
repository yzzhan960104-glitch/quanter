# -*- coding: utf-8 -*-
"""基于 DataLakeReader 的真实数据获取器（与 MockDataFetcher 同协议）。

职责：把 data_lake 真实历史数据以 fetch_ohlcv / fetch_macro 协议暴露给 service 层，
替代 MockDataFetcher 的几何布朗运动假数据，让回测跑在真实历史 K 线上。

设计原则（极简 + 离线降级）：
- 不重新取数：纯读 DataLakeReader 内存湖（启动时 lifespan 已 load 全部湖）。
- 与 MockDataFetcher 同签名（fetch_ohlcv / fetch_macro），service 层零改逻辑、只换实例。
- symbol 路由：'dynamic_top50'（前端 ParamForm 劫持的活跃池代号）→ daily_active 湖活跃池，
  单资产回测取首只代表；真实代码（如 600000.SH）→ 直接 get_timeseries。
- 数据缺失抛 LookupError：由 service 层捕获降级到 MockDataFetcher，保证开发机/CI 无数据湖
  时仍可启动回测（不阻断），并 logger.warning 留痕。

前视偏差红线：
- 不在 fetcher 层做任何重采样/ffill（reader.get_timeseries 返回原始时序，保留停牌空洞）；
  清洗由上层 DataCleaner.clean_ohlcv 统一处理（与 Mock 路径一致），避免在数据获取环节
  引入“用未来解释现在”的污染。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

import pandas as pd

from data.lake_reader import DataLakeReader

logger = logging.getLogger(__name__)

# freq → 湖 key 映射：分钟级走 minute 湖，日级走 daily 湖
_FREQ_TO_LAKE = {
    "1d": "daily",
    "1h": "minute",
    "5m": "minute",
    "1m": "minute",
}

# 宏观 indicator（service 层调用名）→ macro 湖实际列名映射。
# macro 湖由 sync_macro_credit 落盘，列：shrzgm / M1同比增长 / M2同比增长 /
# M0同比增长 / M1M2_gap / dr007（中文列名是 akshare 上游口径，sync 已归一）。
_MACRO_COL_MAP = {
    "m2": "M2同比增长",
    "m1": "M1同比增长",
    "m0": "M0同比增长",
    "m1m2_gap": "M1M2_gap",
    "dr007": "dr007",
    "shrzgm": "shrzgm",
}

# 前端劫持的活跃池代号（ParamForm.vue:387），LakeDataFetcher 内部路由到 daily_active 湖
_ACTIVE_POOL_CODE = "dynamic_top50"


class LakeDataFetcher:
    """真实数据湖获取器（与 MockDataFetcher 同协议）。

    用法：service 层优先用本类；fetch_ohlcv / fetch_macro 抛 LookupError 时降级 MockDataFetcher。
    单例 DataLakeReader 由 lifespan 启动时 load 全部湖，本类只读不写，线程安全。
    """

    def __init__(self) -> None:
        self._reader = DataLakeReader.get_instance()

    # ---------- OHLCV ----------

    def fetch_ohlcv(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        freq: str = "1d",
    ) -> pd.DataFrame:
        """从 data_lake 取真实 OHLCV 时序。

        - 真实 symbol → get_timeseries(symbol, lake=daily/minute 按 freq)
        - 'dynamic_top50' → daily_active 湖活跃池，单资产取首只代表
          （组合回测由 service 逐只调本方法，不走 _resolve_symbol 的单只退化）
        - 数据缺失 → 抛 LookupError（service 降级 Mock）

        返回：OHLCV DataFrame（open/high/low/close/volume/[amount]/[turnover]），
        DatetimeIndex，**不 ffill**（保留停牌空洞交由 DataCleaner 统一清洗）。
        """
        target = self._resolve_symbol(symbol)
        lake = _FREQ_TO_LAKE.get(freq, "daily")
        df = self._reader.get_timeseries(
            target, self._fmt(start), self._fmt(end), lake=lake
        )
        if df is None or df.empty:
            raise LookupError(
                f"data_lake[{lake}] 无 {target} 数据"
                f"（{start.date()}~{end.date()}, freq={freq}）"
            )
        return df

    # ---------- 宏观 ----------

    def fetch_macro(
        self,
        indicator: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """从 macro 湖取宏观指标时序（与 factors/macro_regime.CreditRegime 同源）。

        返回 DataFrame({indicator: series})，列名用请求的 indicator（与 MockDataFetcher
        协议一致），便于策略层透明消费——不必关心 macro 湖的中文列名。
        """
        macro_lake = self._reader._lakes.get("macro")
        if macro_lake is None or macro_lake.empty:
            raise LookupError("macro 湖未加载（data_lake/macro_credit.parquet 缺失？）")
        col = _MACRO_COL_MAP.get(indicator, indicator)
        if col not in macro_lake.columns:
            raise LookupError(
                f"macro 湖无 {indicator} 对应列（候选 {col}，实际列 {list(macro_lake.columns)}）"
            )
        # macro 湖是 DatetimeIndex（单序列），按日期闭区间切片
        series = macro_lake[col].loc[self._fmt(start):self._fmt(end)].dropna()
        if series.empty:
            raise LookupError(
                f"macro 湖 {col} 在 {start.date()}~{end.date()} 区间无数据"
            )
        return series.to_frame(name=indicator)

    # ---------- 活跃池 ----------

    def fetch_active_symbols(self) -> List[str]:
        """daily_active 湖的活跃池标的列表（供组合回测 universe 用）。

        无 daily_active 湖时返空 list（service 决定降级）。
        """
        lake_df = self._reader._lakes.get("daily_active")
        if lake_df is None or lake_df.empty:
            return []
        # MultiIndex(date, symbol) → 取 symbol 层级唯一值（保留出现顺序）
        return list(lake_df.index.get_level_values("symbol").unique())

    # ---------- 内部 ----------

    def _resolve_symbol(self, symbol: str) -> str:
        """symbol 路由：'dynamic_top50' → 活跃池首只（单资产代表）；其余原样。

        Why 首只：单资产回测（BacktestRequest.symbol: str）只跑一只，活跃池多只无法塞进；
        取首只作代表（活跃池已按 momentum 排序，首只是最强标的，代表性强）。
        组合回测走 symbols 列表，由 service 逐只调 fetch_ohlcv，不走此分支。
        """
        if symbol == _ACTIVE_POOL_CODE:
            pool = self.fetch_active_symbols()
            if not pool:
                raise LookupError(
                    "dynamic_top50 路由失败：daily_active 湖未加载"
                    "（先跑 scripts/sync_sector_daily.py）"
                )
            logger.info("dynamic_top50 → 活跃池首只代表 %s", pool[0])
            return pool[0]
        return symbol

    @staticmethod
    def _fmt(dt: datetime) -> str:
        """datetime → 'YYYY-MM-DD' 字符串（reader._norm_date 按湖 dtype 归一化）。"""
        return dt.strftime("%Y-%m-%d")
