# -*- coding: utf-8 -*-
"""backtest/ 回测模块（Layer2 阶段4 · spec §3.6/§5 稳定性隔离）。

物理定位（design §3.6 回测/交易分离 + CLAUDE.md strangler 红线）：
    回测求变、交易求稳，分开避免互相污染。本包收口原散落在 execution/（generic
    回测基础设施：replay/worker/scheduler/tasks_db/runs）+ caisen/optimize/
    （generic 参数训练：training_*）+ trading/mock_broker.py（回测撮合）的回测相关
    代码，独立成 backtest/ 模块。execution/ 包随之解散（其唯一真身即回测基础设施，
    已全部迁入本包；余下 check_exit/check_order/网关等 facade re-export 改由
    trading.* 真身直供，ExecutionExecutor Protocol 迁 trading/protocols.py）。

单向依赖（不变量·spec §3.6 回测不碰交易编排/券商）：
    backtest/ 只依赖 trading.compute（离场判定纯函数）/ strategies（颈线法策略本体）
    / data（行情加载）+ stdlib/pandas。严禁 ``import trading.engine |
    trading.orchestrate | execution | broker``——回测不触盘中执行编排、不接券商 I/O。
    颈线法异步回测链路 intact：optimize.training_loop → tasks_db → worker 内
    neckline 分支调 backtest.replay（策略中立 driver，仅依赖 strategies.base.Strategy）。

公开符号（按子模块分域 re-export）：
    - driver（策略中立回测器）：replay, ReplayReport, ReplayAborted
    - 异步任务生命周期：run_replay_worker, ReplayScheduler
    - 任务表 CRUD：init_db, create_task, get_task, list_tasks, ...
    - 历史结果存取：save_run, list_runs, get_run, delete_run
    - 参数优化子包：backtest.optimize（training_loop/analyzer/loops_db/dingtalk）
    - 回测撮合模拟器：MockBroker
"""
from __future__ import annotations

# ============================================================================
# driver：策略中立回测器（backtest/replay.py）——依赖 strategies.base.Strategy
# 单源真理契约：replay() 与颈线法共用 check_exit（trading.compute.exit 纯函数），
# 杜绝回测/实盘决策分叉（design §3.2 杀手不变量）。
# ============================================================================
from backtest.replay import (  # noqa: F401
    replay,
    ReplayReport,
    ReplayAborted,
)

# ============================================================================
# 异步任务生命周期（backtest/worker.py + backtest/scheduler.py）
# 颈线法经 optimize.training_loop → tasks_db → scheduler → worker 跑参数网格。
# ============================================================================
from backtest.worker import (  # noqa: F401
    run_replay_worker,
)
from backtest.scheduler import (  # noqa: F401
    ReplayScheduler,
)

# ============================================================================
# 任务表 CRUD（backtest/tasks_db.py）——SQLite 持久化，颈线法异步回测的任务队列
# ============================================================================
from backtest.tasks_db import (  # noqa: F401
    init_db,
    create_task,
    get_task,
    list_tasks,
    list_success_runs,
    claim_next_pending,
    update_progress,
    update_heartbeat,
    mark_success,
    mark_failed,
    mark_cancelled,
    delete_task,
    reset_running_to_failed,
)

# ============================================================================
# 历史结果存取（backtest/runs.py）——SUCCESS 回测结果 JSON 落盘 + 检索
# ============================================================================
from backtest.runs import (  # noqa: F401
    save_run,
    list_runs,
    get_run,
    delete_run,
)

# ============================================================================
# 回测撮合模拟器（backtest/mock_broker.py）——MockBroker，回测下单撮合用
# 注意：trading/__init__ 原 ``from .mock_broker import MockBroker`` 已随本迁移移除，
# 消费者改指 ``from backtest import MockBroker``（回测专属，不属交易层）。
# ============================================================================
from backtest.mock_broker import (  # noqa: F401
    MockBroker,
)

__all__ = [
    # driver（策略中立回测器）
    "replay",
    "ReplayReport",
    "ReplayAborted",
    # 异步任务生命周期
    "run_replay_worker",
    "ReplayScheduler",
    # 任务表 CRUD
    "init_db",
    "create_task",
    "get_task",
    "list_tasks",
    "list_success_runs",
    "claim_next_pending",
    "update_progress",
    "update_heartbeat",
    "mark_success",
    "mark_failed",
    "mark_cancelled",
    "delete_task",
    "reset_running_to_failed",
    # 历史结果存取
    "save_run",
    "list_runs",
    "get_run",
    "delete_run",
    # 回测撮合模拟器
    "MockBroker",
]
