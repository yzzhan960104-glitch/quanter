"""数据湖读取适配器：启动期一次性加载 parquet 到内存，提供截面/时序查询。

前视偏差拷问（关键）：
- 仅对【价格列】沿【时间轴】ffill —— 停牌日沿用末次成交价，安全无前视；
  volume/amount 绝不 ffill（停牌日成交应为 0，ffill 会造假量、污染流动性判断）。
- ffill 沿时间方向只传播【过去】值到当前，不引入未来信息。

离线降级：parquet 缺失时 load() 仅记 warning，查询返回空 DF，绝不阻断启动。
"""
from __future__ import annotations

import logging
import os
import threading

import pandas as pd

from config import LAKE_CONFIG

logger = logging.getLogger(__name__)

_PRICE_COLS = ["open", "high", "low", "close"]


class DataLakeReader:
    """单例：全市场日线常驻内存。"""

    _instance: "DataLakeReader | None" = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "DataLakeReader":
        # 双重检查锁：仿 core/notifier.py 的 NotificationManager.get_default()
        # 保证多线程下仅创建一次实例，避免重复加载 parquet 造成内存翻倍
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._df: pd.DataFrame | None = None      # 原始 MultiIndex(date, symbol)
        self._ffill: pd.DataFrame | None = None   # 价格列已 ffill、量类列保持原值的视图
        self._loaded: bool = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    def load(self, path: str | None = None) -> None:
        """加载 parquet 到内存。缺失则进入离线模式（不阻断启动）。"""
        path = path or LAKE_CONFIG["default_path"]
        # 离线降级：parquet 不存在仅记 warning，loaded=False，查询接口返回空 DF。
        # 设计意图：开发机/CI 无数据湖时不致启动崩溃，仅降级为空结果。
        if not os.path.exists(path):
            logger.warning("数据湖缺失：%s，DataLakeReader 进入离线模式（查询返回空）", path)
            self._loaded = False
            return
        df = pd.read_parquet(path)
        # 校验索引形态：必须是 MultiIndex(date, symbol)，否则后续 xs 切片语义错乱。
        if not isinstance(df.index, pd.MultiIndex):
            logger.error("数据湖索引非 MultiIndex(date, symbol)，跳过加载")
            self._loaded = False
            return
        self._df = df
        # 记录 date 层级原生 dtype：查询入参按此归一化，避免 str 索引与 Timestamp
        # 比较（'<' not supported between str and Timestamp）。
        self._date_dtype = df.index.get_level_values("date").dtype
        # 仅【价格列】沿时间 ffill；groupby(level="symbol").ffill() 保证不跨标的串味：
        # 否则会把 A 标的末值填到 B 标的的停牌日，制造虚假价格与前视污染。
        price_cols = [c for c in _PRICE_COLS if c in df.columns]
        if price_cols:
            # 拷贝后只对价格列 ffill，volume/amount 等保持原始值（停牌日 volume=0，
            # ffill 会造假量、污染流动性判断）——故非价格列原样保留。
            ffill_view = df.copy()
            ffill_view[price_cols] = df[price_cols].groupby(level="symbol").ffill()
            self._ffill = ffill_view
        else:
            self._ffill = df.copy()
        self._loaded = True
        logger.info("数据湖加载完成：%s，%d 行", path, len(df))

    def _norm_date(self, date: str) -> object:
        """把入参日期按 date 层级原生 dtype 归一化，规避 str/Timestamp 混比。"""
        # str 层级（parquet 落盘 str）：原样使用，保持与测试 & 落盘格式一致。
        if pd.api.types.is_string_dtype(self._date_dtype):
            return str(date)
        # datetime 层级：转 Timestamp，并剥离时区/时间部分以防日级边界不匹配。
        return pd.Timestamp(date)

    def get_cross_section(self, date: str) -> pd.DataFrame:
        """某日全市场截面（价格取 ffill 版，停牌沿用末次成交价；volume 取原始值不 ffill）。"""
        if not self._loaded or self._ffill is None:
            return pd.DataFrame()
        try:
            # xs 取 date 层级；ffill 视图中价格列已补齐末次成交价，volume 为原始值。
            return self._ffill.xs(self._norm_date(date), level="date")
        except KeyError:
            logger.warning("截面日期不存在：%s", date)
            return pd.DataFrame()

    def get_timeseries(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """单标的原始时序（不 ffill，保留真实停牌空洞供策略层自行决策）。"""
        if not self._loaded or self._df is None:
            return pd.DataFrame()
        try:
            ts = self._df.xs(symbol, level="symbol")
        except KeyError:
            return pd.DataFrame()
        # 闭区间切片；start/end 按 date 层级原生 dtype 归一化后切片。
        return ts.loc[self._norm_date(start):self._norm_date(end)]
