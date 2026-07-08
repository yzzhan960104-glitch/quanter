"""可视化模块：交互式图表

职责：
1. Plotly 交互式图表（Jupyter Notebook 探索）
2. Matplotlib 静态图表（自动化报告）

注：通用回测 HTML/文本报告生成器（viz.report.ReportGenerator）已在
蔡森专精化 Phase 1·Task 4 随 backtest 通用回测引擎整体删除。
蔡森上线前验证由 Phase 2 专用回放验证器承担，不再需要通用报告生成器。
"""

from .interactive import InteractiveChart

__all__ = [
    "InteractiveChart",
]