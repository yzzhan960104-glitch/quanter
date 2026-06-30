"""项目级配置文件

使用纯 Python 字典配置，避免复杂的 YAML/JSON 解析器。

凭证隔离策略：
- API Key / Token 通过 python-dotenv 从 .env 文件加载
- 绝对禁止将凭证硬编码在业务代码中
- 所有数据源模块通过 config 层统一获取凭证，实现单点管控
"""
import os
from datetime import datetime
from typing import Dict, Any, Optional

# 尝试从 .env 文件加载环境变量（开发环境）
# 生产环境应通过系统环境变量或容器 Secret 注入
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv 未安装时，回退到系统环境变量
    pass


# ============================================================
# 数据源凭证（从环境变量加载，绝不硬编码）
# ============================================================
DATA_SOURCE_CREDENTIALS = {
    "fred": {
        # 美联储经济数据 API Key
        "api_key": os.getenv("FRED_API_KEY", ""),
    },
    "tushare": {
        # Tushare Pro Token（A 股数据源）
        "token": os.getenv("TUSHARE_TOKEN", ""),
    },
}


def get_credential(source: str, key: str) -> str:
    """
    安全获取数据源凭证

    参数：
        source: 数据源名称（如 "fred", "tushare"）
        key: 凭证键名（如 "api_key", "token"）

    返回：
        凭证字符串

    异常：
        ValueError: 凭证未配置时抛出，强制开发者显式处理
    """
    cred = DATA_SOURCE_CREDENTIALS.get(source, {}).get(key, "")
    if not cred:
        raise ValueError(
            f"数据源凭证缺失：{source}.{key}。"
            f"请在 .env 文件或系统环境变量中配置 {source.upper()}_{key.upper()}"
        )
    return cred

# 交易时段配置（中国 A 股）
MARKET_HOURS = {
    "morning_start": "09:30",
    "morning_end": "11:30",
    "afternoon_start": "13:00",
    "afternoon_end": "15:00",
}

# 数据源配置
DATA_CONFIG = {
    "default_timezone": "Asia/Shanghai",
    "cache_dir": "data/cache",
    "max_missing_fill": 5,  # 最大前向填充天数（防范停牌期跨度过长）
}

# 回测引擎配置
BACKTEST_CONFIG = {
    "initial_capital": 1_000_000,  # 初始资金 100 万
    "commission_rate": 0.0003,  # 万三佣金
    "stamp_duty": 0.0005,  # 千五印花税（仅卖出）
    "min_commission": 5.0,  # 最低佣金 5 元
    "slippage_model": "linear",  # 滑点模型：linear（线性）/ log（对数）
    "slippage_rate": 0.001,  # 基础滑点率 0.1%
    "liquidity_threshold": 0.02,  # 流动性阈值：成交量 < 平均成交量的 2% 视为流动性枯竭
}

# 因子配置
FACTOR_CONFIG = {
    "ma_short": 5,  # 短均线周期
    "ma_long": 20,  # 长均线周期
    "vpt_window": 20,  # VPT 窗口
    "abnormal_volume_threshold": 5.0,  # 异常成交量阈值（倍数标准差）
    "signal_weights": {"tech": 0.7, "macro": 0.3},  # 信号融合权重
}

# 宏观数据配置（示例）
MACRO_CONFIG = {
    "indicators": ["m2", "cpi", "ppi", "social_financing"],
    "thresholds": {
        "m2": 0.02,  # M2 增速 2% 阈值
        "cpi": 0.03,  # CPI 增速 3% 阈值
    },
    "check_window": 3,  # 连续几期超过阈值触发信号
}

# 可视化配置
VIZ_CONFIG = {
    "chart_theme": "plotly_white",
    "report_dir": "reports",
    "interactive": True,  # 是否生成交互式图表
    "export_formats": ["html"],  # 报告导出格式
}

# Mock 交易配置
MOCK_TRADING_CONFIG = {
    "order_timeout": 300,  # 订单超时时间（秒）
    "partial_fill_enabled": True,  # 是否允许部分成交
    "max_retries": 3,  # 最大重试次数
    "retry_delay": 1.0,  # 重试延迟（秒）
}

# ============================================================
# 工业级蜕变新增配置（纯字典，凭证仍走 .env）
# ============================================================
# 局部别名 _os：与文件顶部已 import 的 os 复用同一模块，此处仅为
# 保持新增配置块的视觉独立性；凭证一律通过 .env / 环境变量注入，
# 业务代码不得在此硬编码任何 Token / API Key。
import os as _os

# 数据湖（Epic 1）
# Parquet 作为 A 股日线落盘格式，列式存储兼顾读写吞吐与内存友好；
# shard_dir 用于按年/月分片，避免单文件膨胀导致读取 OOM。
LAKE_CONFIG = {
    "default_path": _os.getenv("DATA_LAKE_PATH", "data_lake/a_shares_daily.parquet"),
    "shard_dir": "data_lake/shards",
    "years_default": 10,
}

# GLM 大模型（Epic 2）
# 用于新闻/公告情感打分；timeout=15s 防范极端网络抖动下的请求挂死，
# 避免占用回测主循环线程。
LLM_CONFIG = {
    "base_url": _os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
    "model": _os.getenv("ZHIPU_MODEL", "glm-4-flash"),
    "timeout": 15,
}

# 宏观另类数据客户端（Epic 5）
# yfinance_symbols: 标普/原油/黄金/VIX 的 Yahoo Finance 标准代号；
# av_treasury_maturities: Alpha Vantage 美债收益率关键期限，覆盖短端与长端。
MACRO_CLIENT_CONFIG = {
    "yfinance_symbols": {"SPX": "^GSPC", "CL": "CL=F", "GC": "GC=F", "VIX": "^VIX"},
    "av_treasury_maturities": ["3MO", "2Y", "10Y", "30Y"],
}

# Celery 因子沙盒（Epic 3）
# cpu_gate_percent: CPU 占用闸门，超过该阈值则降级/排队，
# 防止因子全量重算压垮实时交易宿主机。
CELERY_CONFIG = {
    "broker_url": _os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    "queue": _os.getenv("CELERY_EXPLORER_QUEUE", "explorer"),
    "cpu_gate_percent": 80.0,
}