# -*- coding: utf-8 -*-
"""infra/ 待迁项（Step4 移出 caisen 包）—— 单向依赖 engines。含 storage/execution/replay/viz。

Step3.4 批 A：storage + execution 物理迁入。re-export 供 ``from caisen.infra import X``
新路径使用；旧路径 ``from caisen import storage`` 经 caisen/__init__ 预加载 +
顶层 sys.modules 别名垫片兜底（Task3.2 沉淀）。
"""
# 批 A re-export（物理迁移自 caisen/storage.py + caisen/execution.py）
from .storage import *  # noqa: F401,F403
from .execution import *  # noqa: F401,F403
# 批 B re-export（物理迁移自 caisen/backtest_replay.py + replay_runs/tasks_db/scheduler/worker.py）
from .backtest_replay import *  # noqa: F401,F403
from .replay_runs import *  # noqa: F401,F403
from .replay_tasks_db import *  # noqa: F401,F403
from .replay_scheduler import *  # noqa: F401,F403
from .replay_worker import *  # noqa: F401,F403
# 批 C re-export（物理迁移自 caisen/viz_static.py + caisen/viz_interactive.py）
from .viz_static import *  # noqa: F401,F403
from .viz_interactive import *  # noqa: F401,F403
