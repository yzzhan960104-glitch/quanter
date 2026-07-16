# -*- coding: utf-8 -*-
"""infra/ 残留垫片层（Step4 整体迁出 caisen 包）—— 仅留转发垫片兜底旧路径。

Step3.4 + Step4 物理迁移整体完成后，本子包下已无实体逻辑——所有模块均已迁出：
  - 批 A（storage + execution）：Step4c 迁入 execution/ 顶层包（execution.storage /
    execution.engine），infra/storage.py + infra/execution.py 降为转发垫片。
  - 批 B（backtest_replay + replay_runs/tasks_db/scheduler/worker）：Step4c 迁入
    execution/ 顶层包（execution.*），infra/replay_*.py + backtest_replay.py 降为垫片。
  - 批 C（viz_static + viz_interactive）：Step4f 迁入横切 viz/ 顶层包
    （viz.viz_static / viz.viz_interactive），infra/viz_*.py 降为垫片。

本 __init__ re-export 仅供 ``from caisen.infra import X`` 这种【包级取属性】形式使用；
``from caisen.infra.X import Y`` 这种【绝对模块路径】形式由各垫片文件的 sys.modules
别名兜底（strangler 铁律①；Task3.2 沉淀）。消费者未切前垫片一律保留（4e 决定）。
"""
# 批 A re-export（真身在 execution/storage + execution/engine）
from .storage import *  # noqa: F401,F403
from .execution import *  # noqa: F401,F403
# 批 B re-export（真身在 execution/backtest_replay + execution/replay_*）
from .backtest_replay import *  # noqa: F401,F403
from .replay_runs import *  # noqa: F401,F403
from .replay_tasks_db import *  # noqa: F401,F403
from .replay_scheduler import *  # noqa: F401,F403
from .replay_worker import *  # noqa: F401,F403
# 批 C re-export（真身在 viz/viz_static + viz/viz_interactive）
from .viz_static import *  # noqa: F401,F403
from .viz_interactive import *  # noqa: F401,F403
