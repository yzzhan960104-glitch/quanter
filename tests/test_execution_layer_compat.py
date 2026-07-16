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


def test_check_exit_single_source():
    """Step4b 契约：check_exit 单源真理——所有路径指向同一函数对象 + backtest_replay 经它离场。

    物理意图（Step4b 核心红线·消除双源真理）：
        Step4b 前：实盘 ExecutionEngine（caisen/infra/execution.py）用 check_exit，
        回放验证器 backtest_replay._simulate_one_trade 用独立内联离场逻辑（无移动止盈）。
        两份各自演化的判定实现构成"回测一套/实盘一套"双源真理——回测调优数据
        可能不反映实盘行为。

        Step4b 抽 check_exit 至 caisen/engines/exit_logic.py（纯逻辑归 engines），
        backtest_replay 改调它。本测试锁：
          1) 源码层 is 同源：caisen.engines.exit_logic.check_exit 与
             caisen.infra.execution.check_exit（经 re-export）是同一函数对象；
          2) 行为层单源：backtest_replay 模块 import 了 check_exit（源码 grep 可证），
             _simulate_one_trade 经 check_exit 离场（engines/exit_logic 是唯一实现）。

    用户决策（已确认）：回测对齐实盘引入移动止盈（trailing_to_breakeven 默认 True），
    接受回测结果变化。trailing 行为变化由 tests/caisen/test_backtest_replay.py
    ::TestStep4bCheckExitSingleSource 守护，本测试只锁单源契约。
    """
    # 1) is 同源：所有路径的 check_exit 指向 caisen/engines/exit_logic.py 的唯一实现
    from caisen.engines.exit_logic import check_exit as engine_check_exit
    from caisen.infra.execution import check_exit as infra_check_exit
    # execution 顶层包（Step4a re-export 自 caisen.infra.execution）
    from execution import check_exit as exec_pkg_check_exit

    assert engine_check_exit is infra_check_exit, (
        "check_exit 双源：engines/exit_logic 与 infra/execution 不同源"
        "（infra/execution 应 re-export 自 engines/exit_logic）"
    )
    assert engine_check_exit is exec_pkg_check_exit, (
        "check_exit 双源：engines/exit_logic 与 execution 顶层包不同源"
    )

    # 2) 行为层单源：backtest_replay import check_exit（确认改调单源函数）
    import caisen.infra.backtest_replay as br_mod
    # _simulate_one_trade 内部 import check_exit（延迟 import），但模块顶层应能解析到同源对象
    # 通过 inspect 源码确认 check_exit 被引用（防回退到内联离场逻辑）
    import inspect
    src = inspect.getsource(br_mod._simulate_one_trade)
    assert "check_exit" in src, (
        "backtest_replay._simulate_one_trade 未调用 check_exit（双源真理回退）"
    )
