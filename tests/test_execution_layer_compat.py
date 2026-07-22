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
    """Step4 初始契约：执行编排相关模块当前路径可 import（迁移后经垫片兜底仍可用）。

    Task 1.3（caisen 形态退役）：execution.engine/storage 真身 + caisen.infra.execution/
    storage 垫片已删。check_exit 由 Task 1.2 迁 execution/exit_logic.py（单源）。本测试
    改为断言 Task 1.3 后存活的执行编排模块可 import。
    """
    # caisen/infra 现有（Task 1.3：仅 replay_* + backtest_replay + viz_*，颈线法异步回测保留）
    import caisen.infra.backtest_replay  # noqa: F401
    import caisen.infra.replay_worker  # noqa: F401
    # trading 现有（执行原语，Step4 保留）
    from trading.execution_gateway import BaseExecutionGateway  # noqa: F401
    from trading.risk_shield import check_order  # noqa: F401
    from trading.qmt_gateway import QmtExecutionGateway  # noqa: F401
    # check_exit 当前位置（Task 1.2：迁 execution/exit_logic.py，单源 is 同源）
    from execution.exit_logic import check_exit, ExitDecision  # noqa: F401


def test_execution_new_path_reexport():
    """Step4 Task4a 契约：execution/ 新顶层包 re-export 公开执行符号 + 与旧路径同源（is 断言）。

    Task 1.3（caisen 形态退役·#3 全删）：ExecutionEngine + storage（save_plans/load_plans/
    get_plan/update_plan 等）已删，不再 re-export。check_exit 由 Task 1.2 迁 execution/
    exit_logic.py（单源）。本测试锁 Task 1.3 后存活的 execution 包 re-export：
      1) 新路径 ``from execution import X`` 可用（import 不抛 + 符号存在）；
      2) 新旧路径返回【同一对象】（is 断言）——确认 re-export 仅引用未复制；
      3) __all__ 非空——公开 API 表面稳定。
    """
    # 1) 新路径 import（覆盖三域：离场判定/回放/网关/风控；Task 1.3：ExecutionEngine/storage 已删）
    from execution import (  # noqa: F401
        check_exit,
        ExitDecision,
        ExitAction,
        ExitReason,
        replay,
        run_replay_worker,
        ReplayScheduler,
        BaseExecutionGateway,
        MockExecutionGateway,
        OrderRequest,
        OrderResult,
        QmtExecutionGateway,
        check_order,
        RiskDecision,
    )

    # 2) 同源断言（is——re-export 不得复制对象）。Task 1.3 后源头改指 execution.exit_logic + trading。
    import execution as exec_pkg
    from execution.exit_logic import check_exit as engine_check_exit
    from execution.exit_logic import ExitDecision as EngineExitDecision
    from trading.execution_gateway import BaseExecutionGateway as LegacyBaseGateway
    from trading.risk_shield import check_order as legacy_check_order
    from trading.qmt_gateway import QmtExecutionGateway as LegacyQmtGateway

    assert check_exit is engine_check_exit, "check_exit 新旧路径不同源"
    assert ExitDecision is EngineExitDecision, "ExitDecision 新旧路径不同源"
    assert BaseExecutionGateway is LegacyBaseGateway, "BaseExecutionGateway 新旧路径不同源"
    assert check_order is legacy_check_order, "check_order 新旧路径不同源"
    assert QmtExecutionGateway is LegacyQmtGateway, "QmtExecutionGateway 新旧路径不同源"

    # 3) __all__ 公开 API 表面非空
    assert len(exec_pkg.__all__) > 0, "execution/__init__ 未声明 __all__（公开 API 表面未锁）"
    # 核心契约符号必须在 __all__ 内（Task 1.3：ExecutionEngine/save_plans 已从表移除）
    for must_have in (
        "check_exit",
        "check_order",
        "BaseExecutionGateway",
        "QmtExecutionGateway",
        "replay",
    ):
        assert must_have in exec_pkg.__all__, f"{must_have} 未列入 execution.__all__"


def test_check_exit_single_source():
    """Step4b 契约：check_exit 单源真理——所有路径指向同一函数对象。

    Task 1.2：caisen 形态退役前置——exit_logic 由 caisen/engines 迁 execution/exit_logic.py
    （杀手不变量·is 同源契约源头改指 execution 包子模块）。
    Task 1.3：ExecutionEngine（实盘 tick_exit 的 check_exit 消费者）随 caisen 形态执行链退役
    删除，但 check_exit 单源契约仍须守护（颈线法回测 backtest_replay 仍依赖 check_exit 离场，
    未来实盘 reducer 也会复用）。caisen.infra.execution 垫片已删（无消费者），同源断言改为
    execution.exit_logic ↔ execution 顶层包两路径。
    """
    # is 同源：execution.exit_logic（真身）与 execution 顶层包 re-export 是同一函数对象
    from execution.exit_logic import check_exit as engine_check_exit
    from execution import check_exit as exec_pkg_check_exit

    assert engine_check_exit is exec_pkg_check_exit, (
        "check_exit 双源：execution.exit_logic 与 execution 顶层包不同源"
        "（顶层包 re-export 应仅引用未复制）"
    )


def test_executor_interface_defined():
    """Step4d 契约：execution/ 定义 ExecutionExecutor Protocol（依赖反转抽象接口）。

    物理意图（Step4d 核心红线·依赖反转 DIP）：
        ExecutionEngine 不再依赖 server.trading_service 具体类型，改依赖 execution/ 内部
        定义的 ExecutionExecutor Protocol（typing.Protocol）。任何提供 get_status +
        submit_order 鸭子类型的对象均可注入（生产 server.trading_service 模块 / 测试
        MagicMock / 未来重写的执行核心）。本测试锁：
          1) execution.execution.interfaces.ExecutionExecutor 存在且是 Protocol 子类；
          2) execution 顶层包 re-export ExecutionExecutor（新路径 ``from execution import
             ExecutionExecutor`` 可用）；
          3) Protocol 声明了 ExecutionEngine 实际调用的两个方法契约（get_status / submit_order）。
    """
    # 1) 接口定义存在 + 是 Protocol
    from execution.interfaces import ExecutionExecutor
    from typing import Protocol
    # Protocol 类的基底能通过 __mro__ 或基类检测识别（runtime_checkable 装饰后仍是 Protocol 子类）
    assert ExecutionExecutor is not None, "ExecutionExecutor 未定义"
    # typing.Protocol 在 Protocol 子类的基底（直接或间接）
    bases = ExecutionExecutor.__mro__
    assert any(b is Protocol or b.__name__ == "Protocol" for b in bases), (
        "ExecutionExecutor 不是 typing.Protocol 子类（依赖反转抽象未正确定义）"
    )

    # 2) execution 顶层包 re-export ExecutionExecutor（新路径可用 + __all__ 锁契约）
    from execution import ExecutionExecutor as ExecFromPkg
    assert ExecFromPkg is ExecutionExecutor, (
        "execution.ExecutionExecutor 与 execution.interfaces.ExecutionExecutor 不同源"
        "（re-export 应引用未复制）"
    )
    import execution as exec_pkg
    assert "ExecutionExecutor" in exec_pkg.__all__, (
        "ExecutionExecutor 未列入 execution.__all__（抽象接口公开 API 表面未锁）"
    )

    # 3) Protocol 声明了 ExecutionEngine 调用的两个方法契约
    # Protocol 的方法契约落在类 __dict__（Protocol 本身的方法定义，非实例）
    assert "get_status" in ExecutionExecutor.__dict__, (
        "ExecutionExecutor 缺 get_status 方法契约（ExecutionEngine 调用面未覆盖）"
    )
    assert "submit_order" in ExecutionExecutor.__dict__, (
        "ExecutionExecutor 缺 submit_order 方法契约（ExecutionEngine 调用面未覆盖）"
    )


def test_execution_no_server_import():
    """Step4d 契约：execution/ 零 ``import server``（strangler 红线·依赖方向单向）。

    物理意图（Step4d 依赖反转 + design §3.1 单向依赖）：
        execution/ 执行编排层绝不反向依赖 server/ 应用层。Step4d 反转 ExecutionEngine 对
        server.trading_service 的依赖为 ExecutionExecutor Protocol 后，engine.py 不再感知
        server 类型。本测试是 grep 绊线——扫描 execution/ 所有 .py 源码，确保无
        ``from server...`` / ``import server`` 字样（反向依赖债复发即红）。

    Step4e 收口：原 replay_worker.py 的 ``from server.services.caisen_service import
    _load_price_data, _merge_cfg`` 反向债已收口——改 import data.price_loader 模块级
    函数（逻辑与 facade 同源单源真理）。白名单已清空，本测试现锁 execution/ 全量零
    ``import server``（无例外）。
    """
    import os
    import re

    execution_dir = os.path.join(os.path.dirname(__file__), "..", "execution")
    execution_dir = os.path.abspath(execution_dir)
    assert os.path.isdir(execution_dir), f"execution/ 目录不存在：{execution_dir}"

    # 反向 import server 的正则（from server / from server.x / import server / import server.x）
    server_import_re = re.compile(r"^\s*(from\s+server|import\s+server)\b", re.MULTILINE)
    # Step4e 后白名单清空：replay_worker 反向债已收口，execution/ 全量零 import server。
    _4E_WHITELIST: dict = {}

    violations = []
    for fname in sorted(os.listdir(execution_dir)):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(execution_dir, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            src = f.read()
        for m in server_import_re.finditer(src):
            reason = _4E_WHITELIST.get(fname)
            if reason:
                # 白名单：记录但不视为违规（保留例外）
                continue
            violations.append((fname, m.group(0).strip(), src.splitlines()[src[:m.start()].count('\n')]))

    assert not violations, (
        "execution/ 存在反向依赖 server（strangler 红线破坏）：\n"
        + "\n".join(f"  {fn}:{ln} -> {line}" for fn, line, ln in violations)
    )


def test_caisen_business_modules_no_reverse_dependency():
    """Step4f 契约：caisen 业务模块零反向依赖 execution/trading/server（收敛终检）。

    物理意图（Step4f 收敛终检·design §3.1 单向依赖）：
        caisen 作为【策略模型层】，业务核心（engines/optimize/advisor）严禁反向
        依赖【执行/服务层】execution/trading/server——这是分层架构的铁律。Step4 把
        infra 整体迁出 caisen 至 execution/ + viz/ 后，本测试做 caisen 收敛终检：
          - 扫描 caisen/engines + caisen/optimize + caisen/advisor 三处【业务模块】的
            源码，确保无 ``from execution|trading|server`` / ``import execution|trading|server``
            字样。
          - 垫片除外：caisen 顶层垫片（caisen/storage.py / caisen/viz_*.py 等）+
            caisen/infra/* 垫片是 strangler 过渡（转发 execution/viz），属 caisen→execution
            依赖但非业务反向，保留（4e 决定·消费者未切）。

    Task 1.3（caisen 形态退役）：caisen/facade.py 已删，业务模块根目录表移除 facade。
    caisen/engines 已空壳（plan/risk/config/patterns 全删），无反向 import 风险。

    已知历史债白名单（4f 终检时记录，非阻塞，待后续清理）：
      - caisen/optimize/training_analyzer.py:18 ``from server.services.review_service
        import _call_glm`` —— Step3.3 沉淀：复用 review_service 的 GLM urllib 调用
        （零新依赖范式），属 Spec3 训练 AI 分析与 review_service 的【横切共享】，
        非业务反向（分析器非策略核心）。后续如需彻底切，把 _call_glm 抽到 data/
        或 utils/ 横切层即可。
    """
    import os
    import re

    # caisen 业务模块根目录（Task 1.3：facade.py 已删，仅 engines/optimize/advisor 三处）
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    business_roots = [
        os.path.join(repo_root, "caisen", "engines"),
        os.path.join(repo_root, "caisen", "optimize"),
        os.path.join(repo_root, "caisen", "advisor"),
    ]
    business_files = []
    for root in business_roots:
        assert os.path.isdir(root), f"业务模块目录不存在：{root}"
        for fname in sorted(os.listdir(root)):
            if fname.endswith(".py"):
                business_files.append(os.path.join(root, fname))

    # 反向 import execution|trading|server 的正则
    reverse_re = re.compile(
        r"^\s*(from\s+(execution|trading|server)|import\s+(execution|trading|server))\b",
        re.MULTILINE,
    )

    # 4f 已知历史债白名单（非业务反向，标注保留待后续清理）
    # Task 1.3：caisen/facade.py 已删（caisen 形态退役），白名单项 ("caisen","facade.py",59) 移除。
    _4F_WHITELIST = {
        # (相对路径, 行号): 原因
        ("caisen", "optimize", "training_analyzer.py", 18): (
            "复用 review_service._call_glm（GLM urllib 三级降级范式），Spec3 训练 AI "
            "分析横切共享，非业务反向；彻底切需把 _call_glm 抽到 data/ 或 utils/。"
        ),
    }

    def _rel_parts(abs_path: str) -> tuple:
        rel = os.path.relpath(abs_path, repo_root)
        parts = tuple(rel.replace("\\", "/").split("/"))
        return parts

    violations = []
    for fpath in business_files:
        if not os.path.isfile(fpath):
            continue
        with open(fpath, "r", encoding="utf-8") as f:
            src = f.read()
        for m in reverse_re.finditer(src):
            # 1-indexed 行号
            line_no = src[:m.start()].count("\n") + 1
            parts = _rel_parts(fpath)
            key = parts + (line_no,)
            if key in _4F_WHITELIST:
                continue  # 白名单：已知历史债，记录不视为违规
            violations.append(
                (os.path.relpath(fpath, repo_root), line_no, m.group(0).strip())
            )

    assert not violations, (
        "caisen 业务模块存在反向依赖 execution/trading/server（分层铁律破坏）：\n"
        + "\n".join(
            f"  {fn}:{ln} -> {line}" for fn, ln, line in violations
        )
        + "\n（caisen/infra + caisen 顶层垫片是 strangler 过渡除外；如新增反向 import "
        "请评估是否应抽横切共享或下沉 schema）"
    )
