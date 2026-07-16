# -*- coding: utf-8 -*-
"""【转发垫片】caisen/viz_interactive.py —— 物理实体已迁至 viz/viz_interactive.py（Step4f 批 C）。

存在原因：server（api/v1/caisen.py 经 ``from caisen.viz_interactive import build_chart_data``
装配 lightweight-charts JSON）+ tests（tests/caisen/test_viz_caisen.py）大量使用
``from caisen.viz_interactive import build_chart_data``，Python 必须能 import 到真实的
``caisen.viz_interactive`` 模块对象。

采用 sys.modules 别名（直指 viz.viz_interactive 单层，防反向 import 循环竞态，与 storage/
execution 顶层垫片同模式 Step4c）：使 ``caisen.viz_interactive`` 与 ``viz.viz_interactive``
成为【同一模块对象】，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价
（strangler 铁律①；Task3.2 沉淀）。

迁移链（三层垫片）：``caisen.viz_interactive``（本顶层垫片，直指真身单层防循环）
→ ``caisen.infra.viz_interactive``（infra 垫片）→ ``viz.viz_interactive``（真身）。
caisen/__init__.py 的 ``from viz.viz_interactive import *`` 预加载使 ``from caisen
import viz_interactive`` 在「垫片先于 viz 包被导入」顺序下仍绑定同一真实模块。

viz_* 属横切可视化层（非策略本体、非执行编排），与顶层 viz/ 合并（design §3.1/§5 F）。
本 viz_interactive（lightweight-charts 契约）与 viz/interactive.py（Plotly InteractiveChart）
是【两套独立可视化组件】，合并后并存不冲突（各自服务不同前端）。

新代码请直接使用 ``from viz.viz_interactive import ...``。
"""
from viz import viz_interactive as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
