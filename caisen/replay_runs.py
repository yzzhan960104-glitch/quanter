# -*- coding: utf-8 -*-
"""【转发垫片】caisen/replay_runs.py —— 物理实体已迁至 execution/replay_runs.py（Step4c 批 B）。

存在原因：facade（Task2.1）+ tests + scripts 大量使用 ``from caisen import replay_runs``
或 ``from caisen.replay_runs import ...``，Python 必须能 import 到真实的
``caisen.replay_runs`` 模块对象。

采用 sys.modules 别名：使 ``caisen.replay_runs`` 与 ``execution.replay_runs`` 成为
【同一模块对象】，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价
（strangler 铁律①；Task3.2 沉淀）。

迁移链与垫片层级（Step4c 沉淀 · 防 execution→caisen 反向 import 循环）：
    本顶层垫片【直指真身】``execution.replay_runs``（单层别名），不经过 caisen.infra.replay_runs
    中间垫片（防 execution/__init__ 触发 caisen 反向 import 时与 caisen.infra 包初始化
    形成循环竞态；与 Step3 ``caisen.plan → caisen.engines.plan`` 单层同模式）。
    ``caisen.infra.replay_runs`` 垫片独立兜底 ``from caisen.infra.replay_runs import X`` 用法。

新代码请直接使用 ``from execution.replay_runs import ...``。
"""
from execution import replay_runs as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
