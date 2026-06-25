"""数据层模块：数据获取与清洗

职责：
1. 定义统一的数据接口契约
2. 实现数据清洗逻辑（异常值处理、缺失值填充）
3. 防范前视偏差（使用发布时间而非数据发生时间）
"""

from .fetcher import DataFetcher, MockDataFetcher
from .cleaner import DataCleaner

__all__ = ["DataFetcher", "MockDataFetcher", "DataCleaner"]