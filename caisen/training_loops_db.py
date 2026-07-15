# -*- coding: utf-8 -*-
"""【转发垫片】caisen/training_loops_db.py —— 物理实体已迁至 caisen/optimize/training_loops_db.py（Step3.3）。

存在原因：server / tests 大量使用 ``from caisen import training_loops_db`` 或
``from caisen.training_loops_db import ...``，Python 必须能 import 到真实的
``caisen.training_loops_db`` 模块对象。

采用 sys.modules 别名：使 ``caisen.training_loops_db`` 与 ``caisen.optimize.training_loops_db``
成为同一模块对象，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价（strangler 铁律①）。

新代码请直接使用 ``from caisen.optimize.training_loops_db import ...``。
"""
from caisen.optimize import training_loops_db as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
