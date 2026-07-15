# -*- coding: utf-8 -*-
"""项目配置包（从原 config.py 857 行上帝文件按归属层拆分）。

归属层映射：
    credentials/market/data/registry → 数据层；macro → 模型层·宏观；
    viz → 横切·可视化；broker → 执行层；celery → 执行编排。

兼容垫片（strangler 铁律①）：保持 `from config import DATASET_REGISTRY`、
`import config; config.LAKE_CONFIG` 等全部旧用法零改动。

dotenv 包入口：load_dotenv() 在此执行一次（包被 import 即触发），
保证所有 credentials 子模块读到 .env 注入的凭证——这是原 config.py 顶部
副作用（行 14-21）的等价迁移，迁移后仍是最早执行点。
"""
# 包入口副作用：加载 .env（原 config.py 行 14-21 等价迁移，最早执行）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from .credentials import DATA_SOURCE_CREDENTIALS, get_credential
from .market import MARKET_HOURS
from .data import (
    DATA_CONFIG, LAKE_CONFIG, MACRO_CLIENT_CONFIG, JQDATA_CONFIG, AKSHARE_CONFIG,
)
from .macro import MACRO_CONFIG
from .viz import VIZ_CONFIG
from .broker import MOCK_TRADING_CONFIG
from .celery import CELERY_CONFIG
from .registry import DATASET_REGISTRY, TUSHARE_DATASETS, SYNCING_DIR

__all__ = [
    "DATA_SOURCE_CREDENTIALS", "get_credential", "MARKET_HOURS", "DATA_CONFIG",
    "LAKE_CONFIG", "MACRO_CLIENT_CONFIG", "JQDATA_CONFIG", "AKSHARE_CONFIG",
    "MACRO_CONFIG", "VIZ_CONFIG", "MOCK_TRADING_CONFIG", "CELERY_CONFIG",
    "DATASET_REGISTRY", "TUSHARE_DATASETS", "SYNCING_DIR",
]
