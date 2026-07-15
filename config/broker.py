# -*- coding: utf-8 -*-
"""Mock 交易配置（执行层）—— 从 config.py 拆出（归属：执行层）。

仅持有订单撮合超时/重试等执行层常量，无外部依赖。
"""
# Mock 交易配置
MOCK_TRADING_CONFIG = {
    "order_timeout": 300,  # 订单超时时间（秒）
    "partial_fill_enabled": True,  # 是否允许部分成交
    "max_retries": 3,  # 最大重试次数
    "retry_delay": 1.0,  # 重试延迟（秒）
}
