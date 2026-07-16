# -*- coding: utf-8 -*-
"""【转发垫片】caisen/storage.py —— 物理实体已迁至 execution/storage.py（Step4c 批 A）。

存在原因：facade（Task2.1）+ caisen/infra/execution.py（execution/engine.py 迁后仍
``from caisen import storage``）+ tests + scripts + server 大量使用
``from caisen import storage`` 或 ``from caisen.storage import ...``，Python 必须能
import 到真实的 ``caisen.storage`` 模块对象。仅靠 caisen/__init__.py 属性赋值无法
满足 ``from caisen.storage import X`` 这种【绝对模块路径】形式。

采用 sys.modules 别名（而非 ``import *``）：使 ``caisen.storage`` 与 ``execution.storage``
成为【同一模块对象】，保证任何基于模块身份的操作（如 monkeypatch 模块全局、tests 中
storage.save_plan 的 mock）在两条路径下完全等价（strangler 铁律①；Task3.2 plan/risk/config
沉淀）。

迁移链与垫片层级（Step4c 沉淀 · 防 execution→caisen 反向 import 循环）：
    本顶层垫片【直指真身】``execution.storage``（单层别名），不经过 caisen.infra.storage
    中间垫片。原因：execution/engine.py 迁后仍 ``from caisen import storage``，若顶层
    垫片经 ``from caisen.infra import storage`` 二跳，会在 execution/__init__ 触发
    caisen 反向 import 时与 caisen.infra 包初始化形成循环竞态（顶层垫片可能捕获到
    未替换的 infra 垫片壳子）。直指 execution.storage 单层别名消除该竞态（与 Step3
    ``caisen.plan → caisen.engines.plan`` 单层同模式）。``caisen.infra.storage`` 垫片
    独立兜底 ``from caisen.infra.storage import X`` 用法（亦直指 execution.storage）。

新代码请直接使用 ``from execution.storage import ...``。
"""
from execution import storage as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
