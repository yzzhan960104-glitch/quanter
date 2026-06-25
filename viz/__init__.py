"""可视化模块：交互式图表与报告生成

职责：
1. Plotly 交互式图表（Jupyter Notebook 探索）
2. Matplotlib 静态图表（自动化报告）
3. 报告生成引擎（HTML/PDF）
"""

from .interactive import InteractiveChart
from .report import ReportGenerator

__all__ = [
    "InteractiveChart",
    "ReportGenerator",
]