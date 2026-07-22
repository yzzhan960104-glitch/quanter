# -*- coding: utf-8 -*-
"""infra/ 残留垫片层（Step4 整体迁出 caisen 包）—— 仅留转发垫片兜底旧路径。

Step3.4 + Step4 物理迁移整体完成后，本子包下已无实体逻辑——所有模块均已迁出：
  - 批 A（storage + execution）：Step4c 迁入 execution/ 顶层包（execution.storage /
    execution.engine）。Task 1.3（caisen 形态退役·#3 全删）：execution.storage +
    execution.engine 真身随 caisen 形态执行链整体退役删除，infra/storage.py +
    infra/execution.py 垫片随之删除（无消费者）。check_exit 已由 Task 1.2 迁
    execution/exit_logic.py（单源），不再经 infra/execution 转发。
  - 批 B（backtest_replay + replay_runs/tasks_db/scheduler/worker）：Step4c 迁入
    execution/ 顶层包（execution.*），infra/replay_*.py + backtest_replay.py 降为垫片。
    Task 1.3：颈线法异步回测基础设施保留，此批垫片保留。
  - 批 C（viz_static + viz_interactive）：Step4f 迁入横切 viz/ 顶层包
    （viz.viz_static / viz.viz_interactive），infra/viz_*.py 降为垫片。

本 __init__ re-export 仅供 ``from caisen.infra import X`` 这种【包级取属性】形式使用；
``from caisen.infra.X import Y`` 这种【绝对模块路径】形式由各垫片文件的 sys.modules
别名兜底（strangler 铁律①；Task3.2 沉淀）。
"""
# Task 1.3：批 A（storage + execution）垫片已删（真身 execution.engine/storage 删）。
# 批 B re-export（真身在 execution/backtest_replay + execution/replay_*，颈线法异步回测保留）
from .backtest_replay import *  # noqa: F401,F403
from .replay_runs import *  # noqa: F401,F403
from .replay_tasks_db import *  # noqa: F401,F403
from .replay_scheduler import *  # noqa: F401,F403
from .replay_worker import *  # noqa: F401,F403
# 批 C re-export（真身在 viz/viz_static + viz/viz_interactive）
from .viz_static import *  # noqa: F401,F403
from .viz_interactive import *  # noqa: F401,F403
