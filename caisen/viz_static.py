# -*- coding: utf-8 -*-
"""【转发垫片】caisen/viz_static.py —— 物理实体已迁至 caisen/infra/viz_static.py（Step3.4 批 C）。

存在原因：tests 大量使用 ``from caisen.viz_static import render_plan_png``，Python
必须能 import 到真实的 ``caisen.viz_static`` 模块对象。

采用 sys.modules 别名：使 ``caisen.viz_static`` 与 ``caisen.infra.viz_static`` 成为
【同一模块对象】，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价
（strangler 铁律①；Task3.2 沉淀）。

新代码请直接使用 ``from caisen.infra.viz_static import ...``（Step4 将迁出 caisen 包）。
"""
from caisen.infra import viz_static as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
