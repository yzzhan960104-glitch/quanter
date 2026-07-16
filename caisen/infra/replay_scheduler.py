# -*- coding: utf-8 -*-
"""【转发垫片】caisen/infra/replay_scheduler.py —— 物理实体已迁至 execution/replay_scheduler.py（Step4c 批 B）。

存在原因：tests + server.main + replay_scheduler 内部动态 import 大量使用
``from caisen.infra.replay_scheduler import ReplayScheduler`` 或 ``from caisen.infra
import replay_scheduler``，Python 必须 import 到真实的 ``caisen.infra.replay_scheduler``
模块对象。

采用 sys.modules 别名：使 ``caisen.infra.replay_scheduler`` 与
``execution.replay_scheduler`` 成为【同一模块对象】，保证 monkeypatch 等基于模块身份
的操作在两条路径下完全等价（strangler 铁律①；Step3 Task3.2 沉淀）。

迁移链（三层垫片）：``caisen.replay_scheduler``（顶层垫片，直指 execution.replay_scheduler
单层别名防反向 import 循环竞态）→ ``caisen.infra.replay_scheduler``（本垫片）
→ ``execution.replay_scheduler``（真身，Step4c 迁入 execution/）。

新代码请直接使用 ``from execution.replay_scheduler import ...``。
"""
from execution import replay_scheduler as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
