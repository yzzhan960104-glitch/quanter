"""横切可视化层（design §3.1）—— Plotly 交互 + lightweight-charts + mplfinance 静态。

职责：
1. 通用回测探索：viz/interactive.py（Plotly InteractiveChart，Jupyter Notebook 探索）。
2. 蔡森形态学·静态图：viz/viz_static.py（mplfinance K线 + alines/hlines 标注 → PNG，
   钉钉/邮件推送，Step4f 自 caisen/infra/viz_static.py 迁入）。
3. 蔡森形态学·交互数据：viz/viz_interactive.py（lightweight-charts JSON 装配，
   build_chart_data 喂前端 /caisen/plans/{plan_id}/chart，Step4f 自 caisen/infra/
   viz_interactive.py 迁入）。

viz_* 属横切可视化层（非策略本体、非执行编排），与通用 viz/interactive.py 合并为
统一横切可视化层（design §5 工作块 F）。viz_interactive（lightweight-charts 契约）
与 interactive（Plotly InteractiveChart）是【两套独立可视化组件】，各自服务不同
前端，并存不冲突。

注：通用回测 HTML/文本报告生成器（viz.report.ReportGenerator）已在蔡森专精化
Phase 1·Task 4 随 backtest 通用回测引擎整体删除。蔡森上线前验证由 Phase 2 专用
回放验证器承担，不再需要通用报告生成器。
"""

from .interactive import InteractiveChart
# 蔡森形态学可视化（Step4f 扁 C 自 caisen/infra/viz_* 迁入）—— 显式 re-export 供
# ``from viz import build_chart_data / render_plan_png`` 用法
from .viz_interactive import build_chart_data  # noqa: F401
from .viz_static import render_plan_png  # noqa: F401

__all__ = [
    "InteractiveChart",
    "build_chart_data",
    "render_plan_png",
]