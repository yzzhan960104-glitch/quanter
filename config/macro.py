# -*- coding: utf-8 -*-
"""宏观数据配置（模型层·宏观）—— 从 config.py 拆出（归属：模型层·宏观）。

仅持有宏观指标列表与阈值常量，无外部依赖。
"""
# 宏观数据配置（示例）
MACRO_CONFIG = {
    "indicators": ["m2", "cpi", "ppi", "social_financing"],
    "thresholds": {
        "m2": 0.02,  # M2 增速 2% 阈值
        "cpi": 0.03,  # CPI 增速 3% 阈值
    },
    "check_window": 3,  # 连续几期超过阈值触发信号
}
