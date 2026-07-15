# -*- coding: utf-8 -*-
"""【转发垫片】screener.py → caisen/engines/patterns/screener.py（Step3.2 批 B）。

采用 sys.modules 别名（而非 ``import *``）：使 ``caisen.patterns.screener`` 与
``caisen.engines.patterns.screener`` 成为【同一模块对象】，保证 monkeypatch 等基于模块
身份的操作（如 tests/caisen/test_screener.py 对 ``PATTERNS`` 的 setattr）在两条路径下
完全等价。strangler 铁律①：旧路径不仅可 import，运行时语义也零差异。
"""
from caisen.engines.patterns import screener as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
