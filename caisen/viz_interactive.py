# -*- coding: utf-8 -*-
"""【转发垫片】caisen/viz_interactive.py —— 物理实体已迁至 caisen/infra/viz_interactive.py（Step3.4 批 C）。

存在原因：server（build_chart_data）+ tests 大量使用
``from caisen.viz_interactive import build_chart_data``，Python 必须能 import 到真实的
``caisen.viz_interactive`` 模块对象。

采用 sys.modules 别名：使 ``caisen.viz_interactive`` 与 ``caisen.infra.viz_interactive``
成为【同一模块对象】，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价
（strangler 铁律①；Task3.2 沉淀）。

新代码请直接使用 ``from caisen.infra.viz_interactive import ...``（Step4 将迁出 caisen 包）。
"""
from caisen.infra import viz_interactive as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
