# -*- coding: utf-8 -*-
"""【转发垫片】caisen/replay_worker.py —— 物理实体已迁至 caisen/infra/replay_worker.py（Step3.4 批 B）。

存在原因：replay_scheduler（动态 import）+ server.main + tests 大量使用
``from caisen.replay_worker import run_replay_worker`` 或 ``from caisen import replay_worker``，
Python 必须能 import 到真实的 ``caisen.replay_worker`` 模块对象。

采用 sys.modules 别名：使 ``caisen.replay_worker`` 与 ``caisen.infra.replay_worker``
成为【同一模块对象】，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价
（strangler 铁律①；Task3.2 沉淀）。

注：实体模块内含反向依赖 ``from server.services.caisen_service import _load_price_data,
_merge_cfg``（Step4 处理），本垫片不改其实体逻辑。

新代码请直接使用 ``from caisen.infra.replay_worker import ...``（Step4 将迁出 caisen 包）。
"""
from caisen.infra import replay_worker as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
