# -*- coding: utf-8 -*-
"""【转发垫片】caisen/execution.py —— 物理实体已迁至 caisen/infra/execution.py（Step3.4 批 A）。

存在原因：tests + scripts + server 大量使用 ``from caisen.execution import ExecutionEngine``
或 ``from caisen import execution as exec_mod``，Python 必须能 import 到真实的
``caisen.execution`` 模块对象。

采用 sys.modules 别名：使 ``caisen.execution`` 与 ``caisen.infra.execution`` 成为
【同一模块对象】，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价
（strangler 铁律①；Task3.2 沉淀）。

新代码请直接使用 ``from caisen.infra.execution import ...``（Step4 将迁出 caisen 包）。
"""
from caisen.infra import execution as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
