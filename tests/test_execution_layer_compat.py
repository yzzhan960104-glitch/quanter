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
