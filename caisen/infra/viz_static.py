# -*- coding: utf-8 -*-
"""【转发垫片】caisen/infra/viz_static.py —— 物理实体已迁至 viz/viz_static.py（Step4f 批 C）。

存在原因：tests（tests/caisen/test_viz_caisen.py 使用 ``from caisen.viz_static import
render_plan_png``）+ facade + server（caisen.py 端点经 caisen.viz_interactive 装配图表）
大量依赖 ``caisen.infra.viz_static`` 或经顶层 ``caisen.viz_static`` 间接走到本垫片，
Python 必须能 import 到真实的 ``caisen.infra.viz_static`` 模块对象。仅靠
caisen/infra/__init__.py 属性赋值无法满足 ``from caisen.infra.viz_static import X``
这种【绝对模块路径】形式。

采用 sys.modules 别名（而非 ``import *``）：使 ``caisen.infra.viz_static`` 与 ``viz.viz_static``
成为【同一模块对象】，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价
（strangler 铁律①；Task3.2 沉淀）。

迁移链（三层垫片）：
    ``caisen.viz_static``（顶层垫片，直指 viz.viz_static 单层别名防反向 import 循环竞态）
    → ``caisen.infra.viz_static``（本垫片）
    → ``viz.viz_static``（真身，Step4f 迁入横切 viz/ 顶层包；mplfinance 静态 PNG 装配）。

viz_* 属横切可视化层（非策略本体、非执行编排），与顶层 viz/（Plotly InteractiveChart 等）
合并为统一横切可视化层（design §3.1 / §5 工作块 F）。

新代码请直接使用 ``from viz.viz_static import ...``。
"""
from viz import viz_static as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
