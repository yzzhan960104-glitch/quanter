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
    from trading.qmt_gateway import QmtExecutionGateway as LegacyQmtGateway

    assert ExecutionEngine is LegacyEngine, "ExecutionEngine 新旧路径不同源（re-export 复制了对象）"
    assert check_exit is legacy_check_exit, "check_exit 新旧路径不同源"
    assert ExitDecision is LegacyExitDecision, "ExitDecision 新旧路径不同源"
    assert BaseExecutionGateway is LegacyBaseGateway, "BaseExecutionGateway 新旧路径不同源"
    assert check_order is legacy_check_order, "check_order 新旧路径不同源"
    assert QmtExecutionGateway is LegacyQmtGateway, "QmtExecutionGateway 新旧路径不同源"

    # 3) __all__ 公开 API 表面非空（4c 物理迁移核对基线）
    assert len(exec_pkg.__all__) > 0, "execution/__init__ 未声明 __all__（公开 API 表面未锁）"
    # 核心契约符号必须在 __all__ 内
    for must_have in (
        "ExecutionEngine",
        "check_exit",
        "check_order",
        "BaseExecutionGateway",
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
        caisen 作为【策略模型层】，业务核心（engines/optimize/facade/advisor）严禁反向
        依赖【执行/服务层】execution/trading/server——这是分层架构的铁律。Step4 把
        infra 整体迁出 caisen 至 execution/ + viz/ 后，本测试做 caisen 收敛终检：
          - 扫描 caisen/engines + caisen/optimize + caisen/facade.py + caisen/advisor
            四处【业务模块】的源码，确保无 ``from execution|trading|server`` / ``import
            execution|trading|server`` 字样。
          - 垫片除外：caisen 顶层垫片（caisen/plan.py / caisen/storage.py / caisen/
            viz_*.py 等）+ caisen/infra/* 垫片是 strangler 过渡（转发 execution/viz），
            属 caisen→execution 依赖但非业务反向，保留（4e 决定·消费者未切）。

    已知历史债白名单（4f 终检时记录，非阻塞，待后续清理）：
      - caisen/optimize/training_analyzer.py:18 ``from server.services.review_service
        import _call_glm`` —— Step3.3 沉淀：复用 review_service 的 GLM urllib 调用
        （零新依赖范式），属 Spec3 训练 AI 分析与 review_service 的【横切共享】，
        非业务反向（分析器非策略核心）。后续如需彻底切，把 _call_glm 抽到 data/
        或 utils/ 横切层即可。
      - caisen/facade.py:59 ``from server.schemas.caisen import CandidatePlan...`` ——
        Step2.1 facade 收口：facade 作为 caisen 模型层对外【唯一契约门面】，对 server
        层暴露 Pydantic schema，是分层边界的【契约出口】（DTO 边界），属历史分层决策
        而非反向债。彻底切需把 schemas/caisen.py 下沉到 caisen/ 内（独立 Task）。
    """
    import os
    import re

    # caisen 业务模块根目录（4 处）
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    business_roots = [
        os.path.join(repo_root, "caisen", "engines"),
        os.path.join(repo_root, "caisen", "optimize"),
        os.path.join(repo_root, "caisen", "advisor"),
    ]
    business_files = [
        os.path.join(repo_root, "caisen", "facade.py"),
    ]
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
    _4F_WHITELIST = {
        # (相对路径, 行号): 原因
        ("caisen", "optimize", "training_analyzer.py", 18): (
            "复用 review_service._call_glm（GLM urllib 三级降级范式），Spec3 训练 AI "
            "分析横切共享，非业务反向；彻底切需把 _call_glm 抽到 data/ 或 utils/。"
        ),
        ("caisen", "facade.py", 59): (
            "facade 收口 server.schemas.caisen Pydantic schema 作为 DTO 边界出口，"
            "属 Step2.1 分层决策；彻底切需把 schemas/caisen.py 下沉到 caisen/。"
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
