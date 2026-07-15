# -*- coding: utf-8 -*-
"""可视化配置（横切·可视化）—— 从 config.py 拆出（归属：横切）。

仅持有报告/图表渲染相关常量，无外部依赖。
"""
# 可视化配置
VIZ_CONFIG = {
    "chart_theme": "plotly_white",
    "report_dir": "reports",
    "interactive": True,  # 是否生成交互式图表
    "export_formats": ["html"],  # 报告导出格式
}
