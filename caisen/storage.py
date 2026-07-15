# -*- coding: utf-8 -*-
"""【转发垫片】caisen/storage.py —— 物理实体已迁至 caisen/infra/storage.py（Step3.4 批 A）。

存在原因：facade（Task2.1）+ tests + scripts + server 大量使用
``from caisen import storage`` 或 ``from caisen.storage import ...``，Python 必须能
import 到真实的 ``caisen.storage`` 模块对象。仅靠 caisen/__init__.py 属性赋值无法
满足 ``from caisen.storage import X`` 这种【绝对模块路径】形式。

采用 sys.modules 别名（而非 ``import *``）：使 ``caisen.storage`` 与 ``caisen.infra.storage``
成为【同一模块对象】，保证任何基于模块身份的操作（如 monkeypatch 模块全局、tests 中
storage.save_plan 的 mock）在两条路径下完全等价（strangler 铁律①；Task3.2 plan/risk/config
沉淀）。配合 caisen/__init__.py 的 ``from caisen.infra.storage import *`` 预加载，使
``from caisen import storage`` 在「垫片先于 infra 包被导入」的顺序下仍绑定同一真实模块。

新代码请直接使用 ``from caisen.infra.storage import ...``（Step4 将迁出 caisen 包）。
"""
from caisen.infra import storage as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
