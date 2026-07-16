# -*- coding: utf-8 -*-
"""【转发垫片】caisen/infra/backtest_replay.py —— 物理实体已迁至 execution/backtest_replay.py（Step4c 批 B）。

存在原因：facade（Task2.1）+ replay_worker + tests + scripts + __main__ + test_shim_identity
_tripwire 大量使用 ``from caisen.infra.backtest_replay import replay, ReplayReport, ReplayAborted``
或 ``from caisen.infra import backtest_replay``，Python 必须 import 到真实的
``caisen.infra.backtest_replay`` 模块对象。

采用 sys.modules 别名（而非 ``import *``）：使 ``caisen.infra.backtest_replay`` 与
``execution.backtest_replay`` 成为【同一模块对象】，保证 monkeypatch 等基于模块身份的操作
在两条路径下完全等价（strangler 铁律①；Step3 Task3.2 沉淀）。配合 caisen/__init__.py
预加载，使 ``from caisen import backtest_replay`` 在「顶层垫片先于 infra 包被导入」顺序下
仍绑定同一真实模块。

迁移链（三层垫片）：``caisen.backtest_replay``（顶层垫片，直指 execution.backtest_replay
单层别名防 execution→caisen 反向 import 循环竞态）→ ``caisen.infra.backtest_replay``（本垫片）
→ ``execution.backtest_replay``（真身，Step4c 迁入 execution/）。

新代码请直接使用 ``from execution.backtest_replay import ...``。
"""
from execution import backtest_replay as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
