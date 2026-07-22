"""横切可视化层（design §3.1）—— 通用 Plotly 交互图表。

职责：
1. 通用回测探索：viz/interactive.py（Plotly InteractiveChart，Jupyter Notebook 探索）。

物理现状（Task 1.4 caisen 形态可视化退役后）：
    - viz_static.py（mplfinance K线 + alines/hlines 标注 → PNG，喂 caisen 形态 plan 可视化）
      已删：pattern 可视化是 caisen 形态专属 dead code（live 调用方只有已删的 server
      api/v1/caisen + facade + test_viz_caisen，Task 1.1/1.4 全删）。
    - viz_interactive.py（lightweight-charts JSON 装配 build_chart_data，喂前端
      /caisen/plans/{plan_id}/chart）已删：同属 caisen 形态可视化 dead code。
    - interactive.py（Plotly InteractiveChart，通用回测净值/回撤/信号图）保留：
      与 caisen 形态无关，tests/test_viz.py 覆盖。

注：通用回测 HTML/文本报告生成器（viz.report.ReportGenerator）已在蔡森专精化
Phase 1·Task 4 随 backtest 通用回测引擎整体删除。
"""

from .interactive import InteractiveChart

__all__ = [
    "InteractiveChart",
]
