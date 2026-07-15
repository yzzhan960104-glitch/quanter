# -*- coding: utf-8 -*-
"""【转发垫片】caisen/risk.py —— 物理实体已迁至 caisen/engines/risk.py（Step3.2 批 A）。

存在原因：内部模块（plan/patterns/screener）+ tests + scripts + server 大量使用
``from caisen.risk import RiskManager`` 这种【绝对模块路径】，Python 必须能 import 到
真实的 ``caisen.risk`` 模块对象。

采用 sys.modules 别名：使 ``caisen.risk`` 与 ``caisen.engines.risk`` 成为同一模块对象，
保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价（strangler 铁律①）。

新代码请直接使用 ``from caisen.engines.risk import RiskManager``。
"""
from caisen.engines import risk as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
