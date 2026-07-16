# -*- coding: utf-8 -*-
"""execution/ 执行编排层（Step4 · Task 4a · 骨架）。

物理定位（design §3.1 + CLAUDE.md strangler 红线）：
    本包是 Step4 抽出的【执行编排层】顶层包，承载蔡森形态学流水线 Phase 3 的"盘中
    执行状态机 + 回放验证 + 三执行态网关（Mock/EMT/QMT）+ 风控挡板"。与 caisen/
    包【单向依赖】：execution → caisen.engines / caisen.infra / trading（现状），
    绝不反向。4c 物理迁移完成后，依赖将反转为 execution ← caisen.infra（执行原语
    下沉到本包，caisen 仅保留策略本体），届时本包的 re-export 源头改为本包子模块。

当前态（Task 4a · 骨架 re-export）：
    物理文件【尚未迁移】（Step4 strangler 铁律①：新旧并存）。本 __init__ 仅做
    re-export 转发——从 caisen.infra.* 与 trading.* 现位置【单向 re-export】公开
    执行相关符号，让新路径 ``from execution import ExecutionEngine, check_order,
    BaseExecutionGateway, ...`` 可用。旧路径 ``from caisen.infra.execution import X``
    / ``from trading.execution_gateway import Y`` 仍完全可用（零改动）。

    Task 4c 物理迁移（``git mv caisen/infra/execution.py execution/engine.py`` 等）
    完成后：本 __init__ 的 re-export 源头由 caisen.infra.* 改指本包子模块（.engine /
    .storage / .replay / .gateway_* / .risk_shield），caisen/infra 侧改用垫片
    反向兜底（与 Step3 Task3.2 caisen顶层垫片 同模式，strangler 铁律①）。

单向依赖 / 无循环 import 证明（红线）：
    - 本包 re-export 自 caisen.infra.execution / backtest_replay / storage / replay_*。
    - caisen.infra.* / caisen/__init__ 当前【无任何】对 execution 顶层包的引用
      （grep `import execution` / `from execution` 在 caisen/ 下零命中）。
    - trading/__init__ 仅导入 mock_broker / order_state / qmt_gateway，不触 execution。
    ⇒ execution → caisen.infra → caisen.engines 是单向链，无 execution 回指。
    4c 物理迁移后此链反转，届时 caisen.infra 经 sys.modules 垫片兜底（避免循环）。

公开符号（按 design §3.1 分域 re-export）：
    - 引擎/离场判定：ExecutionEngine, check_exit, ExitDecision, ExitAction, ExitReason
    - 回放验证：replay, ReplayReport, ReplayAborted
    - 持久化：save_plans, load_plans, get_plan, update_plan, load_active_plans,
              add_to_cooldown, in_cooldown
    - 回放任务/调度：run_replay_worker, ReplayScheduler, save_run, list_runs, get_run,
                     delete_run, init_db, create_task, get_task, list_tasks 等
    - 三执行态网关：BaseExecutionGateway, MockExecutionGateway, OrderRequest, OrderResult,
                    reconcile, PositionDrift, ReconciliationResult, EmtExecutionGateway,
                    QmtExecutionGateway
    - 风控挡板：check_order, RiskDecision
"""
from __future__ import annotations

# ============================================================================
# 执行器抽象接口（Step4d 依赖反转：ExecutionEngine 依赖此接口非 server.trading_service）
# ============================================================================
from .interfaces import (  # noqa: F401
    ExecutionExecutor,
)

# ============================================================================
# 引擎 + 离场纯函数（Step4c 批 A：物理迁入本包 execution/engine.py）
# ExecutionEngine 状态机 + re-export 自 caisen.engines.exit_logic 的 check_exit（Step4b 单源）。
# ============================================================================
from .engine import (  # noqa: F401
    ExecutionEngine,
    check_exit,
    ExitDecision,
    ExitAction,
    ExitReason,
)

# ============================================================================
# 回放验证器（Step4c 批 B：物理迁入本包 execution/backtest_replay.py）
# 单源真理契约：replay() 与 ExecutionEngine 共用 check_exit，杜绝双源真理（design §3.2）。
# ============================================================================
from .backtest_replay import (  # noqa: F401
    replay,
    ReplayReport,
    ReplayAborted,
)

# ============================================================================
# 计划持久化 + 冷却黑名单（Step4c 批 A：物理迁入本包 execution/storage.py）
# ============================================================================
from .storage import (  # noqa: F401
    save_plans,
    load_plans,
    get_plan,
    update_plan,
    load_active_plans,
    add_to_cooldown,
    in_cooldown,
)

# ============================================================================
# 回放任务生命周期（Step4c 批 B：物理迁入本包 execution/replay_*.py）
# 保留 caisen.infra 侧垫片兜底 ``from caisen.infra.replay_* import X`` 用法。
# ============================================================================
from .replay_worker import (  # noqa: F401
    run_replay_worker,
)
from .replay_runs import (  # noqa: F401
    save_run,
    list_runs,
    get_run,
    delete_run,
)
from .replay_tasks_db import (  # noqa: F401
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
from .replay_scheduler import (  # noqa: F401
    ReplayScheduler,
)

# ============================================================================
# 三执行态网关（trading.execution_gateway —— Mock/EMT/QMT 三个网关 + 订单原语）
# 物理位置保留 trading/（执行原语层，Step4 不迁入 execution 包，design §3.1）。
# ============================================================================
from trading.execution_gateway import (  # noqa: F401
    BaseExecutionGateway,
    MockExecutionGateway,
    OrderRequest,
    OrderResult,
    reconcile,
    PositionDrift,
    ReconciliationResult,
)
from trading.emt_gateway import (  # noqa: F401
    EmtExecutionGateway,
)
from trading.qmt_gateway import (  # noqa: F401
    QmtExecutionGateway,
)

# ============================================================================
# 风控挡板（trading.risk_shield —— 下单前 10 关校验，纯函数）
# ============================================================================
from trading.risk_shield import (  # noqa: F401
    check_order,
    RiskDecision,
)

# ----------------------------------------------------------------------------
# __all__：显式声明本包公开 API 表面（pylint/mypy 友好 + 4c 迁移后单点核对）。
# 只列入"稳定执行原语"，私有/内部符号（_ 前缀）不导出。
# ----------------------------------------------------------------------------
__all__ = [
    # 执行器抽象接口（Step4d 依赖反转）
    "ExecutionExecutor",
    # 引擎 + 离场判定
    "ExecutionEngine",
    "check_exit",
    "ExitDecision",
    "ExitAction",
    "ExitReason",
    # 回放验证
    "replay",
    "ReplayReport",
    "ReplayAborted",
    # 计划持久化 + 冷却
    "save_plans",
    "load_plans",
    "get_plan",
    "update_plan",
    "load_active_plans",
    "add_to_cooldown",
    "in_cooldown",
    # 回放任务生命周期
    "run_replay_worker",
    "save_run",
    "list_runs",
    "get_run",
    "delete_run",
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
    "ReplayScheduler",
    # 三执行态网关
    "BaseExecutionGateway",
    "MockExecutionGateway",
    "OrderRequest",
    "OrderResult",
    "reconcile",
    "PositionDrift",
    "ReconciliationResult",
    "EmtExecutionGateway",
    "QmtExecutionGateway",
    # 风控挡板
    "check_order",
    "RiskDecision",
]
