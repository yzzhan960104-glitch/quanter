# -*- coding: utf-8 -*-
"""Layer2 依赖契约测试（阶段6 · spec §7 依赖铁律固化）。

物理意图（Why 本测试存在）：
    前五阶段把 caisen 上帝包 + execution 拆解散、broker/backtest/trading 分离、
    trading 五层（types/compute/state/io/orchestrate）定型。本测试把 spec §7 的
    【可静态 import 扫描】铁律固化成 CI 可查断言——防止后续 strangler 反向回流
    （如 broker 偷偷 import trading.engine 编排、strategies 偷偷 import broker
    下单），一旦回流即回测与实盘分叉、循环依赖炸全仓。

扫描机制（Why AST 而非正则）：
    用 ``ast.parse`` 精确提取 ``ast.Import`` / ``ast.ImportFrom`` 节点，
    天然跳过 docstring/注释/字符串字面量（正则易误伤中文文档）。每个 .py 文件
    单独 parse，违规信息精确到 ``文件:行:import 原文``。

覆盖的 spec §7 铁律（6 条）：
    1. trading/compute + trading/state：纯决策层，零 broker/data/io/orchestrate/
       execution/requests/xtquant（functional core 杀手不变量）。
    2. strategies：策略层，零 trading/broker/execution/backtest（策略只产 Signal，
       不触碰执行）。
    3. broker：券商叶子，零 trading.engine/orchestrate/signal_runner/
       dynamic_whitelist/risk_shield/stop_loss/circuit_breaker（无反向依赖交易编排）。
       允许 trading.compute / trading.types / trading.order_state（纯契约）/ data。
    4. backtest：回测层，零 trading.engine/orchestrate/broker/execution（回测只经
       compute/strategies/data/infra）。AI 分析横切走 infra/llm（Layer2 follow-up #3
       已收口：原 backtest→server.services.review_service 反向依赖下沉为 backtest→infra
       正向依赖，故不再需 training_analyzer 白名单）。
    5. experiment：纯配置叶子，零任何 Layer2 兄弟（trading/strategies/broker/
       backtest/caisen/execution）+ server。
    6. trading/io + trading/orchestrate：禁业务判定（启发式 grep，warning 不 fail）。

注：test_compute_purity.py（阶段2 建）保留为 compute 子集专用契约（含 is 同源断言），
本文件是其全包泛化版——两者互补，不重复（compute_purity 守 is 同源，layer_contract
守跨包铁律）。

Layer2 阶段6 垫片清/留决策（spec §7 strangler 铁律①）：
    已删（消费点≤2 且无 monkeypatch，消费点迁真身）：
      - trading/risk_shield.py    → 真身 trading.compute.risk（server/trading_service + test_risk_shield 迁）
      - trading/stop_loss.py      → 真身 trading.compute.stop（test_stop_loss 迁）
      - trading/circuit_breaker.py → 真身 trading.compute.breaker + trading.io.breaker（零外部消费）
    保留（消费点多/有 monkeypatch/hybrid 真身，留 stage6 follow-up）：
      - trading/signal_runner.py     → 真身 trading.compute.plan（engine.py + 4 tests 含 e2e 不 mock）
      - trading/execution_gateway.py → 真身 broker.base/mock + compute.reconcile/types（20+ 消费 spec §101）
      - trading/order_state.py       → hybrid：OrderStateMachine 真身 + OrderState/check_* re-export（broker/backtest 依赖枚举）
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# 仓库根目录（tests/ 的父目录）
REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# AST 扫描基础工具
# ============================================================================

def _collect_imports(file_path: Path) -> list[tuple[int, str, str]]:
    """解析单个 .py 文件，返回 [(lineno, module_root, raw_text), ...]。

    module_root：被 import 的顶层包名（如 ``from trading.compute import X`` → "trading"；
    ``from broker.base import Y`` → "broker"）。裸 ``import a.b`` 同样取 "a"。
    raw_text：``line.strip()`` 原文，用于失败信息展示。

    Why AST：精确跳过 docstring/注释/字符串字面量（正则扫描会误伤中文文档中
    形如 ``from trading.risk_shield import check_order`` 的示例代码块）。
    """
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except SyntaxError:
        # 语法错误文件不计入契约扫描（应由其他测试守护）——避免本测试因语法噪声 fail
        return []

    imports: list[tuple[int, str, str]] = []
    source_lines = file_path.read_text(encoding="utf-8").splitlines()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                raw = source_lines[node.lineno - 1].strip() if node.lineno - 1 < len(source_lines) else f"import {alias.name}"
                imports.append((node.lineno, root, raw))
        elif isinstance(node, ast.ImportFrom):
            # 相对导入（from . import X / from .backtest import Y，node.level>0）跳过：
            # 相对导入的目标在【本包内部】（如 strategies/neckline/__init__.py 的
            # ``from .backtest import`` 实指 strategies.neckline.backtest，非顶层 backtest 包），
            # 不构成跨包依赖——契约铁律管的是跨包边界，包内 re-export 自由。
            if node.level > 0:
                continue
            if node.module:  # from . import X（module=None）的相对导入顶层为 None，跳过
                root = node.module.split(".")[0]
                raw = source_lines[node.lineno - 1].strip() if node.lineno - 1 < len(source_lines) else f"from {node.module} import ..."
                imports.append((node.lineno, root, raw))
    return imports


def _iter_py_files(package_dir: Path) -> list[Path]:
    """枚举包目录下所有 .py（含子目录，排除 __pycache__）。"""
    if not package_dir.exists():
        return []
    return sorted(
        p for p in package_dir.rglob("*.py")
        if "__pycache__" not in str(p)
    )


def _check_forbidden(
    package_dir: Path,
    package_label: str,
    forbidden_roots: set[str],
    *,
    forbidden_subpaths: tuple[str, ...] = (),
    allowlist_files: tuple[str, ...] = (),
) -> list[str]:
    """通用禁止校验：扫描 package_dir 下所有 .py 的 import，断言不命中禁止集。

    参数：
        package_dir：被扫描的包目录（绝对路径）。
        package_label：包的人类可读名（用于失败信息，如 "trading/compute"）。
        forbidden_roots：禁止的【顶层包】集合（如 {"broker", "execution", "requests"}）。
        forbidden_subpaths：禁止的【子路径前缀】元组（如 ("trading.io", "trading.orchestrate")），
            用于禁止某包的特定子包但允许该包其他部分（如 compute 允许 trading.compute /
            trading.types，但禁 trading.io / trading.orchestrate）。
        allowlist_files：显式豁免的文件相对路径元组（相对仓库根，正斜杠分隔），
            如 ("backtest/optimize/training_analyzer.py",)。

    返回：违规信息列表（空则通过）。
    """
    violations: list[str] = []
    for mod_path in _iter_py_files(package_dir):
        rel = mod_path.relative_to(REPO_ROOT).as_posix()
        if rel in allowlist_files:
            continue  # 白名单豁免（如 training_analyzer 的 AI 横切依赖）
        for lineno, root, raw in _collect_imports(mod_path):
            # ① 顶层包命中禁止集
            if root in forbidden_roots:
                violations.append(
                    f"{package_label} 铁律违反 @ {rel}:{lineno}: 禁止 import 顶层包 "
                    f"{root!r} ← {raw}"
                )
                continue
            # ② 子路径前缀命中（处理 ``from trading.io import X`` 这种顶层包合法但
            #    子包越界的情况——root="trading" 不在 forbidden_roots，但 "trading.io"
            #    命中 forbidden_subpaths）
            for sub in forbidden_subpaths:
                # raw 形如 "from trading.io.breaker import cancel_all_open_orders"
                # 用正则提取 from 后的模块全名做前缀匹配
                m = re.match(r"^\s*(?:from\s+|import\s+)([a-zA-Z_][\w.]*)", raw)
                if m and (m.group(1) == sub or m.group(1).startswith(sub + ".")):
                    violations.append(
                        f"{package_label} 铁律违反 @ {rel}:{lineno}: 禁止 import 子路径 "
                        f"{sub!r} ← {raw}"
                    )
                    break
    return violations


# ============================================================================
# 铁律 1：trading/compute + trading/state 纯决策层零外部依赖
# ============================================================================

# functional core 黑名单（spec §7.1）：任何 I/O / 执行编排符号进 compute 即破坏
# 「回测与实盘共用同一份决策」的杀手不变量。
_COMPUTE_FORBIDDEN_ROOTS = {
    "broker", "data", "execution", "requests", "xtquant", "xttrader",
    "aiohttp", "websockets", "asyncio", "sqlite3",
}
# trading 子包黑名单：compute 允许 trading.compute（自身）/ trading.types（纯数据契约）/
# strategies（frozen Signal），但禁 trading.io / trading.orchestrate（I/O 壳 + 编排）。
_COMPUTE_FORBIDDEN_SUBPATHS = ("trading.io", "trading.orchestrate")


def test_compute_subpackage_pure() -> None:
    """铁律 1a：trading/compute/ 零 broker/data/execution/io/orchestrate/requests/xtquant 依赖。

    物理意义：functional core 必须是纯函数——无 I/O、无状态、确定性。一旦 compute
    偷偷 import broker，回测跑的 compute 版与实盘跑的 compute 版会因 broker 状态
    分叉而给出不同决策（经典「回测对、实盘翻车」）。
    """
    violations = _check_forbidden(
        REPO_ROOT / "trading" / "compute",
        "trading/compute",
        _COMPUTE_FORBIDDEN_ROOTS,
        forbidden_subpaths=_COMPUTE_FORBIDDEN_SUBPATHS,
    )
    assert not violations, "\n".join(violations)


def test_state_subpackage_pure() -> None:
    """铁律 1b：trading/state/ 零 broker/data/execution/io/orchestrate/requests/xtquant 依赖。

    物理意义：state 层是 reducer 式薄壳（颈线法靠 broker 跟踪状态），必须保持
    纯净——不直接调 I/O，状态变更由 orchestrate 编排驱动。
    """
    violations = _check_forbidden(
        REPO_ROOT / "trading" / "state",
        "trading/state",
        _COMPUTE_FORBIDDEN_ROOTS,
        forbidden_subpaths=_COMPUTE_FORBIDDEN_SUBPATHS,
    )
    assert not violations, "\n".join(violations)


# ============================================================================
# 铁律 2：strategies 零交易/券商/执行依赖
# ============================================================================

# 策略层黑名单：strategies 只产 Signal（frozen dataclass），不触碰下单/撮合/回测。
# 一旦策略 import broker，策略就与特定券商耦合，无法在回测中替换 mock。
_STRATEGIES_FORBIDDEN_ROOTS = {"trading", "broker", "execution", "backtest"}


def test_strategies_no_trade_dependency() -> None:
    """铁律 2：strategies/ 零 trading/broker/execution/backtest 依赖。

    物理意义：策略是「信号生成器」，输出 frozen Signal dataclass；它不应知道
    信号如何被执行、撮合、回测。一旦策略 import trading.engine，策略就与特定
    执行引擎耦合，回测时无法替换。
    """
    violations = _check_forbidden(
        REPO_ROOT / "strategies",
        "strategies",
        _STRATEGIES_FORBIDDEN_ROOTS,
    )
    assert not violations, "\n".join(violations)


# ============================================================================
# 铁律 3：broker 零反向依赖交易编排
# ============================================================================

# broker 是券商叶子包（I/O 适配层）。允许：trading.compute（OrderRequest 等纯契约）/
# trading.types / trading.order_state（OrderState 枚举）/ data。
# 禁止：trading 的任何【编排/状态/风控判定】模块——broker 不应反向依赖交易编排，
# 否则形成 trading↔broker 循环依赖。
# 注：Layer2 阶段6 已删 risk_shield/stop_loss/circuit_breaker 垫片，故此处不再
# 列这三者（路径已不存在，broker 无法 import）。signal_runner 垫片保留（消费多），
# 故仍列为禁止子路径——防 broker 反向依赖。
_BROKER_FORBIDDEN_SUBPATHS = (
    "trading.engine",
    "trading.orchestrate",
    "trading.signal_runner",
    "trading.dynamic_whitelist",
)


def test_broker_no_reverse_trade_dependency() -> None:
    """铁律 3：broker/ 零 trading.engine/orchestrate/signal_runner/dynamic_whitelist
    反向依赖。

    物理意义：broker 是 I/O 适配层（下单/撤单/查持仓的券商封装），它不应反向
    依赖交易的编排逻辑。允许 broker import trading.compute.types（OrderRequest
    纯契约）/ trading.order_state（OrderState 枚举）/ data（行情数据）——这些是
    纯数据契约，不构成反向编排依赖。

    违反此铁律会形成 trading↔broker 循环依赖，导致 import 炸全仓（如早期
    trading/__init__.py eager import broker.qmt 触发的 partially-initialized 循环）。

    Layer2 阶段6 变更：risk_shield/stop_loss/circuit_breaker 垫片已删（路径不存在，
    无需列入禁止）；signal_runner 垫片保留故仍禁。
    """
    # 注意：broker 的 forbidden_roots 为空——broker 允许 import trading（但仅限
    # 纯契约子路径），所以只用 forbidden_subpaths 精确拦截编排模块。
    violations = _check_forbidden(
        REPO_ROOT / "broker",
        "broker",
        forbidden_roots=set(),
        forbidden_subpaths=_BROKER_FORBIDDEN_SUBPATHS,
    )
    assert not violations, "\n".join(violations)


# ============================================================================
# 铁律 4：backtest 零 trading.engine/orchestrate/broker/execution 依赖
# ============================================================================

# 回测层黑名单：回测只经 trading.compute（纯决策）/ trading.types / trading.order_state
# （纯契约）/ strategies（策略）/ data（数据）/ infra（外部依赖适配层，如 LLM/通知）。
# 禁止 broker/execution（实盘执行）/ trading.engine/orchestrate（实盘编排）——
# 回测求变、实盘求稳，物理隔离。
_BACKTEST_FORBIDDEN_ROOTS = {"broker", "execution"}
_BACKTEST_FORBIDDEN_SUBPATHS = ("trading.engine", "trading.orchestrate")
# 注：原 _BACKTEST_ALLOWLIST（豁免 backtest/optimize/training_analyzer →
# server.services.review_service 的 AI 横切依赖）已在 Layer2 follow-up #3 收口后
# 删除——training_analyzer 改走 infra/llm，不再 import server，契约随之收紧。
# 历史 stage6 的 allowlist 设计（让收口目标显眼）已完成它的使命：现在 backtest
# 零 server 依赖被本契约硬守护，任何回流会立即 fail。


def test_backtest_no_live_execution_dependency() -> None:
    """铁律 4：backtest/ 零 trading.engine/orchestrate/broker/execution 依赖。

    物理意义：回测是「历史的离线重放」，必须与实盘执行物理隔离——回测经
    MockBroker 撮合，实盘经 broker.qmt 下单。一旦回测 import broker，回测就
    可能误触真实下单（灾难性）。

    允许的正向依赖：trading.compute / trading.types / trading.order_state（纯契约）/
    strategies（策略）/ data（数据）/ infra（外部依赖适配层——LLM/通知等横切，
    与 data 同属「外部世界适配」，不构成实盘执行反向依赖）。

    Layer2 follow-up #3 变更：原 ``backtest/optimize/training_analyzer.py`` 反向
    import ``server.services.review_service`` 的 AI 横切依赖已收口为
    ``infra.llm``（ports & adapters）——backtest→infra 是合法正向依赖，本测试
    自动放行；同时不再需要 allowlist 豁免（契约较 stage6 更紧）。
    """
    violations = _check_forbidden(
        REPO_ROOT / "backtest",
        "backtest",
        _BACKTEST_FORBIDDEN_ROOTS,
        forbidden_subpaths=_BACKTEST_FORBIDDEN_SUBPATHS,
    )
    assert not violations, "\n".join(violations)


# ============================================================================
# 铁律 5：experiment 纯配置叶子零外部依赖
# ============================================================================

# experiment 是纯配置叶子包（实盘策略版本管理 + 权重）。禁任何 Layer2 兄弟 +
# server——experiment 是最外层配置，不应反向依赖被它配置的兄弟。
_EXPERIMENT_FORBIDDEN_ROOTS = {
    "trading", "strategies", "broker", "backtest",
    "caisen", "execution", "server",
}


def test_experiment_pure_leaf() -> None:
    """铁律 5：experiment/ 零任何 Layer2 兄弟(trading/strategies/broker/backtest/
    caisen/execution) + server 依赖。

    物理意义：experiment 是纯配置叶子（实盘版本 + 权重管理），它定义「当前跑哪个
    策略版本、权重多少」。它不应 import 被它配置的兄弟（trading/strategies 等），
    否则配置层反向依赖执行层，形成环。experiment 应只依赖标准库 + 自身。
    """
    violations = _check_forbidden(
        REPO_ROOT / "experiment",
        "experiment",
        _EXPERIMENT_FORBIDDEN_ROOTS,
    )
    assert not violations, "\n".join(violations)


# ============================================================================
# 铁律 6：trading/io + trading/orchestrate 禁业务判定（启发式 · warning 级）
# ============================================================================

# io/orchestrate 是 imperative shell（副作用壳 + 编排），它们「只连线不判定」。
# 但「判定逻辑」是运行时行为，静态 import 扫描难精确捕获——用启发式 grep
# 搜常见判定模式作为【软警告】（默认跳过，仅在显式调用时跑）。
#
# 启发式模式：形如 ``if X.should_stop`` / ``if check_exit(...)`` / ``if check_order(...)``
# 出现在 io/orchestrate 的非注释行，疑似业务判定泄漏到 shell 层。
_DECISION_PATTERN = re.compile(
    r"^\s*if\s+.*\b(should_stop|check_exit|check_order|check_stop_loss|check_take_profit)\b"
)


def _scan_decision_patterns(package_dir: Path) -> list[str]:
    """启发式扫描 io/orchestrate 下疑似业务判定的 if 语句（软警告，不硬 fail）。"""
    warnings: list[str] = []
    for mod_path in _iter_py_files(package_dir):
        rel = mod_path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(
            mod_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _DECISION_PATTERN.match(line):
                warnings.append(
                    f"{rel}:{lineno}: 疑似业务判定泄漏到 shell 层 ← {stripped}"
                )
    return warnings


def test_io_orchestrate_no_business_decision_warning() -> None:
    """铁律 6（软警告）：trading/io + trading/orchestrate 不应含业务判定 if。

    物理意图：io/orchestrate 是 imperative shell，职责是「只连线不判定」——
    下单/撤单/查持仓的 I/O 编排，判定逻辑应下沉 trading.compute（functional core）。

    本测试是【启发式 grep】，默认收集 warning 但不硬 fail（判定模式难以精确区分
    「编排中的守卫 if」与「业务判定泄漏」）。当 warning 列表增长时，应人工 review
    是否有判定逻辑回流 shell 层。

    如需激活硬 fail（收紧契约），取消下方 assert 注释即可。
    """
    warnings: list[str] = []
    for sub in ("io", "orchestrate"):
        warnings.extend(_scan_decision_patterns(REPO_ROOT / "trading" / sub))
    # 当前基线：无 warning（io/orchestrate 已抽干净）。若未来出现 warning，先 review，
    # 确认是真业务判定泄漏后再取消下行注释转为硬 fail。
    # assert not warnings, "io/orchestrate 疑似业务判定泄漏：\n" + "\n".join(warnings)
    # stage6 基线：记录但不阻断（任务明确「warning 不硬 fail」）
    if warnings:
        pytest.skip(
            f"io/orchestrate 出现 {len(warnings)} 处疑似业务判定（启发式 warning，"
            f"需人工 review 是否真泄漏）：\n" + "\n".join(warnings)
        )
