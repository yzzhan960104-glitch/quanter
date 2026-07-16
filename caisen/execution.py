# -*- coding: utf-8 -*-
"""【转发垫片】caisen/execution.py —— 物理实体已迁至 execution/engine.py（Step4c 批 A）。

存在原因：tests + scripts + server + tests/test_execution_layer_compat.py + tests/
test_layering_compat::test_shim_identity_tripwire 大量使用
``from caisen.execution import ExecutionEngine`` 或 ``from caisen import execution as exec_mod``，
Python 必须能 import 到真实的 ``caisen.execution`` 模块对象。

采用 sys.modules 别名（而非 ``import *``）：使 ``caisen.execution`` 与 ``execution.engine``
成为【同一模块对象】，保证 check_exit 单源真理契约（Step4b：engines/exit_logic ↔
infra/execution ↔ execution 顶层包三者 is 同源）在迁后仍成立（strangler 铁律①；
Task3.2 沉淀）。

迁移链与垫片层级（Step4c 沉淀 · 防 execution→caisen 反向 import 循环）：
    本顶层垫片【直指真身】``execution.engine``（单层别名），不经过 caisen.infra.execution
    中间垫片。原因：execution/engine.py 迁后仍 ``from caisen import storage``，若顶层
    垫片经 ``from caisen.infra import execution`` 二跳，会在 execution/__init__ 触发
    caisen 反向 import 时与 caisen.infra 包初始化形成循环竞态。直指 execution.engine
    单层别名消除该竞态（与 Step3 ``caisen.plan → caisen.engines.plan`` 单层同模式）。
    ``caisen.infra.execution`` 垫片独立兜底 ``from caisen.infra.execution import X`` 用法
    （亦直指 execution.engine）。

新代码请直接使用 ``from execution.engine import ...`` 或 ``from execution import
ExecutionEngine, check_exit, ...``（顶层包 re-export）。
"""
from execution import engine as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
