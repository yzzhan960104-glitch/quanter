"""项目级配置文件

使用纯 Python 字典配置，避免复杂的 YAML/JSON 解析器。
"""
from datetime import datetime
from typing import Dict, Any

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