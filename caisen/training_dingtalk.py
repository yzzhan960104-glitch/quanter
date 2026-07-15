# -*- coding: utf-8 -*-
"""【转发垫片】caisen/training_dingtalk.py —— 物理实体已迁至 caisen/optimize/training_dingtalk.py（Step3.3）。

存在原因：server / tests / scripts 大量使用 ``from caisen.training_dingtalk import
ReviewBotConfig, DingTalkNotifier`` 或 ``from caisen import training_dingtalk``，Python
必须能 import 到真实的 ``caisen.training_dingtalk`` 模块对象。

采用 sys.modules 别名：使 ``caisen.training_dingtalk`` 与 ``caisen.optimize.training_dingtalk``
成为同一模块对象，保证 monkeypatch 等基于模块身份的操作（如 test_training_loop 对
``caisen.training_dingtalk`` 的 mock）在两条路径下完全等价（strangler 铁律①）。

新代码请直接使用 ``from caisen.optimize.training_dingtalk import ...``。
"""
from caisen.optimize import training_dingtalk as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
