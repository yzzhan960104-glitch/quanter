# -*- coding: utf-8 -*-
"""【转发垫片】caisen/replay_runs.py —— 物理实体已迁至 caisen/infra/replay_runs.py（Step3.4 批 B）。

存在原因：facade（Task2.1）+ tests + scripts 大量使用 ``from caisen import replay_runs``
或 ``from caisen.replay_runs import ...``，Python 必须能 import 到真实的
``caisen.replay_runs`` 模块对象。

采用 sys.modules 别名：使 ``caisen.replay_runs`` 与 ``caisen.infra.replay_runs`` 成为
【同一模块对象】，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价
（strangler 铁律①；Task3.2 沉淀）。

新代码请直接使用 ``from caisen.infra.replay_runs import ...``（Step4 将迁出 caisen 包）。
"""
from caisen.infra import replay_runs as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
