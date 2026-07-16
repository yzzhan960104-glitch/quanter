# -*- coding: utf-8 -*-
"""【转发垫片】caisen/infra/storage.py —— 物理实体已迁至 execution/storage.py（Step4c 批 A）。

存在原因：facade（Task2.1）+ caisen/infra/execution.py + tests + scripts + server 大量使用
``from caisen.infra import storage`` 或 ``from caisen.infra.storage import ...``，Python 必须
能 import 到真实的 ``caisen.infra.storage`` 模块对象。仅靠 caisen/infra/__init__.py 属性
赋值无法满足 ``from caisen.infra.storage import X`` 这种【绝对模块路径】形式。

采用 sys.modules 别名（而非 ``import *``）：使 ``caisen.infra.storage`` 与 ``execution.storage``
成为【同一模块对象】，保证任何基于模块身份的操作（如 monkeypatch 模块全局、tests 中
storage.save_plan 的 mock）在两条路径下完全等价（strangler 铁律①；Step3 Task3.2 plan/risk/config
沉淀）。配合 caisen/__init__.py 的 ``from execution.storage import *`` 预加载，使
``from caisen import storage`` 在「顶层垫片先于 infra 包被导入」的顺序下仍绑定同一真实模块。

迁移链（三层垫片）：``caisen.storage``（顶层垫片，直指 execution.storage 单层别名防反向
import 循环竞态）→ ``caisen.infra.storage``（本垫片）→ ``execution.storage``（真身，
Step4c 迁入 execution/ 执行编排层顶层包）。

新代码请直接使用 ``from execution.storage import ...``。
"""
from execution import storage as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
