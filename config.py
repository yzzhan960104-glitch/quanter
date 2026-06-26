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