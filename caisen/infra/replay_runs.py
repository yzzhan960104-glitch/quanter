# -*- coding: utf-8 -*-
"""【转发垫片】caisen/infra/replay_runs.py —— 物理实体已迁至 execution/replay_runs.py（Step4c 批 B）。

存在原因：facade（Task2.1）+ tests + scripts + test_shim_identity_tripwire 大量使用
``from caisen.infra.replay_runs import save_run, list_runs, ...`` 或 ``from caisen.infra
import replay_runs``，Python 必须 import 到真实的 ``caisen.infra.replay_runs`` 模块对象。

采用 sys.modules 别名：使 ``caisen.infra.replay_runs`` 与 ``execution.replay_runs`` 成为
【同一模块对象】，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价
（strangler 铁律①；Step3 Task3.2 沉淀）。

迁移链（三层垫片）：``caisen.replay_runs``（顶层垫片，直指 execution.replay_runs 单层别名
防反向 import 循环竞态）→ ``caisen.infra.replay_runs``（本垫片）→ ``execution.replay_runs``
（真身，Step4c 迁入 execution/）。

新代码请直接使用 ``from execution.replay_runs import ...``。
"""
from execution import replay_runs as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
