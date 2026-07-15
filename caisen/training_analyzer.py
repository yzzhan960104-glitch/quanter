# -*- coding: utf-8 -*-
"""【转发垫片】caisen/training_analyzer.py —— 物理实体已迁至 caisen/optimize/training_analyzer.py（Step3.3）。

存在原因：server / tests / scripts 大量使用 ``from caisen.training_analyzer import ...``
或 ``from caisen import training_analyzer as ta``，Python 必须能 import 到真实的
``caisen.training_analyzer`` 模块对象。

采用 sys.modules 别名：使 ``caisen.training_analyzer`` 与 ``caisen.optimize.training_analyzer``
成为同一模块对象，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价（strangler 铁律①；
Task3.2 plan/risk/config 沉淀，非 import *，避免两副本不同对象致 monkeypatch 失效）。

新代码请直接使用 ``from caisen.optimize.training_analyzer import ...``。
"""
from caisen.optimize import training_analyzer as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
