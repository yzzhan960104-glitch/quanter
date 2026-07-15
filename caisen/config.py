# -*- coding: utf-8 -*-
"""【转发垫片】caisen/config.py —— 物理实体已迁至 caisen/engines/config.py（Step3.2 批 A）。

存在原因：内部模块（plan/risk/patterns/*）+ tests + scripts + server 大量使用
``from caisen.config import StrategyConfig`` 这种【绝对模块路径】，Python 必须能 import 到
一个真实的 ``caisen.config`` 模块对象。仅靠 caisen/__init__.py 属性赋值无法满足该形式。

采用 sys.modules 别名（而非 ``import *``）：使 ``caisen.config`` 与 ``caisen.engines.config``
成为【同一模块对象】，保证任何基于模块身份的操作（如 monkeypatch 模块全局）在两条路径下
完全等价。strangler 铁律①：旧路径不仅可 import，运行时语义也零差异。

新代码请直接使用 ``from caisen.engines.config import StrategyConfig``。
"""
from caisen.engines import config as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
