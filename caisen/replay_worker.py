# -*- coding: utf-8 -*-
"""【转发垫片】caisen/replay_worker.py —— 物理实体已迁至 execution/replay_worker.py（Step4c 批 B）。

存在原因：replay_scheduler（动态 import）+ server.main + tests 大量使用
``from caisen.replay_worker import run_replay_worker`` 或 ``from caisen import replay_worker``，
Python 必须能 import 到真实的 ``caisen.replay_worker`` 模块对象。

采用 sys.modules 别名：使 ``caisen.replay_worker`` 与 ``execution.replay_worker`` 成为
【同一模块对象】，保证 monkeypatch（如 _load_price_data/_merge_cfg 替换）等基于模块身份
的操作在两条路径下完全等价（strangler 铁律①；Task3.2 沉淀）。

注：实体模块原内含反向依赖 ``from server.services.caisen_service import _load_price_data,
_merge_cfg``（Step4e 已收口——改 import data.price_loader 模块级函数，消除反向依赖）。

迁移链与垫片层级（Step4c 沉淀 · 防 execution→caisen 反向 import 循环）：
    本顶层垫片【直指真身】``execution.replay_worker``（单层别名），不经过
    caisen.infra.replay_worker 中间垫片（防循环竞态；与 Step3 ``caisen.plan →
    caisen.engines.plan`` 单层同模式）。``caisen.infra.replay_worker`` 垫片独立兜底
    ``from caisen.infra.replay_worker import X`` 用法。

新代码请直接使用 ``from execution.replay_worker import ...``。
"""
from execution import replay_worker as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
