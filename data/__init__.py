"""数据层模块：数据获取与清洗

职责：
1. 定义统一的数据接口契约
2. 实现数据清洗逻辑（异常值处理、缺失值填充）
3. 防范前视偏差（使用发布时间而非数据发生时间）
4. 双轨数据源接入：FRED 宏观锚点 + Tushare A 股基本面
5. 组合模式聚合网关：统一路由与 tz-aware 保障

【边界声明·Step1】data/ = 取数代码包（clients/fetcher/cleaner/resilience/
tushare_sync/lake_reader），只放 .py 代码，不放数据文件。物理 parquet 存储在
data_lake/（见 data_lake/.README）。二者命名相似但职责正交，勿混。
"""

from .fetcher import (
    DataFetcher,
    MockDataFetcher,
    FredDataFetcher,
    TushareDataFetcher,
    CompositeDataFetcher,
)
from .cleaner import DataCleaner

__all__ = [
    "DataFetcher",
    "MockDataFetcher",
    "FredDataFetcher",
    "TushareDataFetcher",
    "CompositeDataFetcher",
    "DataCleaner",
]