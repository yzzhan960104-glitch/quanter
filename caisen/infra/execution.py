# -*- coding: utf-8 -*-
"""【转发垫片】caisen/infra/execution.py —— 物理实体已迁至 execution/engine.py（Step4c 批 A）。

存在原因：tests + scripts + server + tests/test_execution_layer_compat.py + tests/test_shim
_identity_tripwire 大量使用 ``from caisen.infra.execution import ExecutionEngine, check_exit,
ExitDecision, ExitAction, ExitReason`` 或 ``from caisen.infra import execution``，Python 必须
能 import 到真实的 ``caisen.infra.execution`` 模块对象。仅靠 caisen/infra/__init__.py 属性
赋值无法满足 ``from caisen.infra.execution import X`` 这种【绝对模块路径】形式。

采用 sys.modules 别名（而非 ``import *``）：使 ``caisen.infra.execution`` 与
``execution.engine`` 成为【同一模块对象】，保证 check_exit 单源真理契约（Step4b：
engines/exit_logic ↔ infra/execution ↔ execution 顶层包三者 is 同源）在迁后仍成立
（strangler 铁律①；Step3 Task3.2 沉淀）。配合 caisen/__init__.py 的
``from execution.engine import *`` 预加载，使 ``from caisen import execution`` 在
「顶层垫片先于 infra 包被导入」的顺序下仍绑定同一真实模块。

迁移链（三层垫片）：``caisen.execution``（顶层垫片，直指 execution.engine 单层别名防反向
import 循环竞态）→ ``caisen.infra.execution``（本垫片）→ ``execution.engine``（真身，
Step4c 迁入 execution/ 执行编排层顶层包；ExecutionEngine 状态机 + re-export 自
caisen.engines.exit_logic 的 check_exit/ExitDecision/ExitAction/ExitReason）。

新代码请直接使用 ``from execution.engine import ...`` 或 ``from execution import
ExecutionEngine, check_exit, ...``（顶层包 re-export）。
"""
from execution import engine as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
