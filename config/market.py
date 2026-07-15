# -*- coding: utf-8 -*-
"""交易时段配置（数据层·市场口径）—— 从 config.py 拆出（归属：数据层）。

仅持有 A 股交易时段常量，无外部依赖。
"""
# 交易时段配置（中国 A 股）
MARKET_HOURS = {
    "morning_start": "09:30",
    "morning_end": "11:30",
    "afternoon_start": "13:00",
    "afternoon_end": "15:00",
}
