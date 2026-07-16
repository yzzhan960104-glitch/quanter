# -*- coding: utf-8 -*-
"""Step4 执行编排层兼容契约测试（贯穿 4a-4f）。

物理意图：Step4 把 caisen/infra 整体迁出 caisen 包至 execution/ 执行编排层，
消除 check_exit 双源真理，反转 ExecutionEngine 反向依赖。本文件是 strangler
安全网——每个子阶段追加契约断言（旧路径可用 + 新路径同源 + caisen 零反向依赖绊线）。

设计纪律：只做 import 与符号存在/同源断言，不做业务行为（行为由既有 test_backtest_replay/
test_execution/test_risk_shield 等守护）。
"""
from __future__ import annotations


def test_execution_layer_legacy_paths_importable():
    """Step4 初始契约：执行编排相关模块当前路径可 import（迁移后经垫片兜底仍可用）。"""
    # caisen/infra 现有（Step4c 迁 execution/ 后经垫片兜底）
    import caisen.infra.execution  # noqa: F401
    import caisen.infra.backtest_replay  # noqa: F401
    import caisen.infra.storage  # noqa: F401
    import caisen.infra.replay_worker  # noqa: F401
    # trading 现有（执行原语，Step4 保留）
    from trading.execution_gateway import BaseExecutionGateway  # noqa: F401
    from trading.risk_shield import check_order  # noqa: F401
    from trading.emt_gateway import EmtExecutionGateway  # noqa: F401
    from trading.qmt_gateway import QmtExecutionGateway  # noqa: F401
    # check_exit 当前位置（Step4b 抽到 engines/exit_logic 后经兜底仍可用）
    from caisen.infra.execution import check_exit, ExitDecision  # noqa: F401


def test_execution_new_path_reexport():
    """Step4 Task4a 契约：execution/ 新顶层包 re-export 公开执行符号 + 与旧路径同源（is 断言）。

    物理意图：Step4 strangler 铁律①（新旧并存）。4a 只建 execution/__init__.py 骨架，
    物理文件未迁，新路径经 re-export 指向 caisen.infra.* / trading.* 现位置。本测试锁：
      1) 新路径 ``from execution import X`` 可用（import 不抛 + 符号存在）；
      2) 新旧路径返回【同一对象】（is 断言）——确认 re-export 仅引用未复制，
         4c 物理迁移后此断言仍须成立（垫片须指向迁后真实模块）；
      3) __all__ 非空——公开 API 表面稳定（4c 迁移核对基线）。
    """
    # 1) 新路径 import（覆盖三域：引擎/网关/风控）
    from execution import (  # noqa: F401
        ExecutionEngine,
        check_exit,
        ExitDecision,
        ExitAction,
        ExitReason,
        replay,
        save_plans,
        load_plans,
        get_plan,
        update_plan,
        run_replay_worker,
        ReplayScheduler,
        BaseExecutionGateway,
        MockExecutionGateway,
        OrderRequest,
        OrderResult,
        EmtExecutionGateway,
        QmtExecutionGateway,
        check_order,
        RiskDecision,
    )

    # 2) 同源断言（is——re-export 不得复制对象）。横跨 caisen.infra + trading 两源。
    import execution as exec_pkg
    from caisen.infra.execution import (
        ExecutionEngine as LegacyEngine,
        check_exit as legacy_check_exit,
        ExitDecision as LegacyExitDecision,
    )
    from trading.execution_gateway import BaseExecutionGateway as LegacyBaseGateway
    from trading.risk_shield import check_order as legacy_check_order
    from trading.emt_gateway import EmtExecutionGateway as LegacyEmtGateway
    from trading.qmt_gateway import QmtExecutionGateway as LegacyQmtGateway

    assert ExecutionEngine is LegacyEngine, "ExecutionEngine 新旧路径不同源（re-export 复制了对象）"
    assert check_exit is legacy_check_exit, "check_exit 新旧路径不同源"
    assert ExitDecision is LegacyExitDecision, "ExitDecision 新旧路径不同源"
    assert BaseExecutionGateway is LegacyBaseGateway, "BaseExecutionGateway 新旧路径不同源"
    assert check_order is legacy_check_order, "check_order 新旧路径不同源"
    assert EmtExecutionGateway is LegacyEmtGateway, "EmtExecutionGateway 新旧路径不同源"
    assert QmtExecutionGateway is LegacyQmtGateway, "QmtExecutionGateway 新旧路径不同源"

    # 3) __all__ 公开 API 表面非空（4c 物理迁移核对基线）
    assert len(exec_pkg.__all__) > 0, "execution/__init__ 未声明 __all__（公开 API 表面未锁）"
    # 核心契约符号必须在 __all__ 内
    for must_have in (
        "ExecutionEngine",
        "check_exit",
        "check_order",
        "BaseExecutionGateway",
        "EmtExecutionGateway",
        "QmtExecutionGateway",
        "replay",
        "save_plans",
    ):
        assert must_have in exec_pkg.__all__, f"{must_have} 未列入 execution.__all__"
