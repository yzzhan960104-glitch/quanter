# -*- coding: utf-8 -*-
"""【转发垫片】caisen/infra/replay_tasks_db.py —— 物理实体已迁至 execution/replay_tasks_db.py（Step4c 批 B）。

存在原因：facade（Task2.1）+ replay_scheduler + replay_worker + caisen.optimize.training_loop
+ caisen.optimize.training_loops_db + tests + scripts + server + test_shim_identity_tripwire
大量使用 ``from caisen.infra.replay_tasks_db import _now_iso, _connect, _DEFAULT_DB_PATH,
init_db, create_task, ...`` 或 ``from caisen.infra import replay_tasks_db``，Python 必须
import 到真实的 ``caisen.infra.replay_tasks_db`` 模块对象。

采用 sys.modules 别名：使 ``caisen.infra.replay_tasks_db`` 与 ``execution.replay_tasks_db``
成为【同一模块对象】，保证 monkeypatch（如 _DEFAULT_DB_PATH 替换做测试隔离）等基于模块
身份的操作在两条路径下完全等价（strangler 铁律①；Step3 Task3.2 沉淀）。

迁移链（三层垫片）：``caisen.replay_tasks_db``（顶层垫片，直指 execution.replay_tasks_db
单层别名防反向 import 循环竞态）→ ``caisen.infra.replay_tasks_db``（本垫片）
→ ``execution.replay_tasks_db``（真身，Step4c 迁入 execution/）。

新代码请直接使用 ``from execution.replay_tasks_db import ...``。
"""
from execution import replay_tasks_db as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
