# -*- coding: utf-8 -*-
"""【转发垫片】caisen/plan.py —— 物理实体已迁至 caisen/engines/plan.py（Step3.2 批 A）。

存在原因：内部模块 + tests + scripts + server 大量使用 ``from caisen.plan import TradePlan``
或 ``from caisen import plan as plan_mod``，Python 必须能 import 到真实的 ``caisen.plan``
模块对象。

采用 sys.modules 别名：使 ``caisen.plan`` 与 ``caisen.engines.plan`` 成为同一模块对象，
保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价（strangler 铁律①）。

新代码请直接使用 ``from caisen.engines.plan import ...``。
"""
from caisen.engines import plan as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
