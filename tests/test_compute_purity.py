# -*- coding: utf-8 -*-
"""trading.compute 子包纯净契约测试（Layer2 阶段2 · spec §3.5/§4）。

守护核心不变量：trading/compute/ 是 functional core（无 I/O、无状态、确定性），
回测与实盘共用同一套决策逻辑（杀手不变量）。本测试以两种互补方式守护：

1. 【静态 · import 扫描】grep 扫描 trading/compute/ 下所有 .py 的 import 语句，
   禁止出现任何 I/O / 副作用 / 执行编排依赖。允许：标准库 / pandas / numpy /
   dataclasses / typing / strategies（Signal 等 frozen dataclass 纯数据契约）/
   trading.types（若建）/ trading.compute 内部互引。

   Why 静态扫描而非 ast：strangler 模式下文件频繁增减，静态正则扫描对文件
   增删零维护成本（ast 需维护符号表）；正则已足够覆盖 ``from X import`` /
   ``import X`` 两形。

2. 【同源 is 契约】迁移后多入口指向同一函数对象——``trading.compute.X.fn`` 与
   原路径垫片（trading.<old_module>.fn / execution.fn）必须 ``is`` 同一对象。
   这是 strangler 铁律①（搬迁非复制）的硬验证：若误把函数复制而非 git mv，
   回测调 compute 版、实盘调原版，会出现"看似都用同名函数、行为却分叉"的
   隐性双源真理。
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

# trading/compute/ 子包根目录（绝对路径，跨平台安全）
COMPUTE_DIR = Path(__file__).resolve().parent.parent / "trading" / "compute"

# ============================================================================
# 1. 静态 import 扫描：trading/compute/ 下零外部 I/O 依赖
# ============================================================================

# 允许 import 的白名单前缀（命中其一即放行）。覆盖：标准库 / numpy / pandas /
# strategies（frozen dataclass 纯数据契约）/ trading.compute 内部互引 / trading.types。
_ALLOWED_PREFIXES = (
    # Python 标准库（白名单充分但非穷举——新增标准库在此追加）
    "os", "sys", "math", "enum", "typing", "dataclasses", "abc",
    "collections", "functools", "itertools", "datetime", "decimal",
    "pathlib", "re", "json", "copy", "__future__",
    # 第三方纯计算库（无 I/O）
    "numpy", "pandas",
    # 项目内纯数据契约
    "strategies",          # Signal 等 frozen dataclass（无 I/O）
    "trading.types",       # 若建（纯类型契约）
    "trading.compute",     # 本子包内部互引
)

# 显式黑名单：任何形态出现即 fail（即使前缀巧合命中白名单也兜底拦截）。
# 物理意义：这些符号都是 I/O 或执行编排——一旦进 compute 就破坏 functional core 纯净。
_FORBIDDEN_PATTERNS = (
    re.compile(r"\bbroker\b"),                # 券商 I/O 适配
    re.compile(r"\bdata\b(?!classes)"),       # data 数据采集包（不误伤 dataclasses）
    re.compile(r"\bexecution\b"),             # execution 执行编排层
    re.compile(r"\btrading\.io\b"),           # trading.io 子包
    re.compile(r"\btrading\.orchestrate\b"),  # trading.orchestrate 子包
    re.compile(r"\brequests\b"),              # HTTP I/O
    re.compile(r"\bxtquant\b"),               # QMT 底层 I/O
    re.compile(r"\bxttrader\b"),              # QMT 下单 I/O
    re.compile(r"\baiohttp\b"),               # 异步 HTTP I/O
    re.compile(r"\bwebsockets\b"),            # WebSocket I/O
    re.compile(r"\basyncio\b"),               # 事件循环（决策函数应同步纯函数）
    re.compile(r"\bsqlite3\b"),               # DB I/O
    re.compile(r"\bopen\("),                  # 文件 I/O（裸 open）
)

# 形如 ``from X import ...`` / ``import X [, Y]`` 的 import 行（支持多行 from ... ( )）
_FROM_IMPORT_RE = re.compile(r"^\s*from\s+([a-zA-Z_][\w.]*)\s+import\b")
_PLAIN_IMPORT_RE = re.compile(r"^\s*import\s+([a-zA-Z_][\w.\s,]*)")


def _iter_compute_modules() -> list[Path]:
    """枚举 trading/compute/ 下所有 .py 文件（含 __init__.py，排除 __pycache__）。"""
    if not COMPUTE_DIR.exists():
        return []
    return sorted(p for p in COMPUTE_DIR.rglob("*.py") if "__pycache__" not in str(p))


def _extract_imported_roots(line: str) -> list[str]:
    """从一行代码提取被 import 的顶层包名（含 from X / import X[, Y] 两形）。"""
    roots: list[str] = []
    m = _FROM_IMPORT_RE.match(line)
    if m:
        roots.append(m.group(1).split(".")[0])
        return roots
    m = _PLAIN_IMPORT_RE.match(line)
    if m:
        # import a, b.c, d → 拆出每个根
        for tok in m.group(1).split(","):
            tok = tok.strip()
            if tok:
                roots.append(tok.split(".")[0])
    return roots


def test_compute_subpackage_has_no_external_io_dependencies() -> None:
    """契约：trading/compute/ 下所有 .py 的【import 语句】仅来自白名单，零 I/O / 执行编排依赖。

    扫描对象：仅 ``from X import ...`` / ``import X`` 两形的 import 行（不扫 docstring /
    注释 / 函数体内的字面量——这些不是真依赖，扫描它们会误伤中文文档）。

    失败时打印违规文件+行+import 内容，便于定位 strangler 搬运时漏切的 I/O。
    """
    violations: list[str] = []
    for mod_path in _iter_compute_modules():
        rel = mod_path.relative_to(COMPUTE_DIR.parent.parent)
        text = mod_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            # 仅对真实 import 行做校验（_extract_imported_roots 仅匹配 import 形）
            roots = _extract_imported_roots(line)
            if not roots:
                continue  # 非 import 行（含 docstring/代码体），跳过——避免字面量误伤
            # 双重校验：① 黑名单兜底（防白名单遗漏新出现的 I/O 库）② 白名单显式允许
            for root in roots:
                # 黑名单：命中任何 I/O / 执行编排模式即记录（root 本身就是违规包）
                for pat in _FORBIDDEN_PATTERNS:
                    if pat.search(root):
                        violations.append(
                            f"{rel}:{lineno}: 黑名单命中 {pat.pattern!r}（root={root!r}）← {line.strip()}"
                        )
                # 白名单：顶层包必须命中白名单前缀（否则即使不在黑名单也拒——未登记的新依赖）
                if not any(root == p or root.startswith(p + ".") or root == p.split(".")[0]
                           for p in _ALLOWED_PREFIXES):
                    if root in {p.split(".")[0] for p in _ALLOWED_PREFIXES}:
                        continue
                    violations.append(
                        f"{rel}:{lineno}: 非白名单 import 根 {root!r} ← {line.strip()}"
                    )
    assert not violations, (
        "trading/compute/ 违反零外部依赖契约——以下 import 破坏 functional core 纯净：\n"
        + "\n".join(violations)
    )


# ============================================================================
# 2. 同源 is 契约：迁移后多入口指向同一函数对象
# ============================================================================
@pytest.mark.parametrize(
    "module_path, attr",
    [
        # check_exit：双入口同源（compute / compute 包 re-export）
        # Layer2 阶段4：execution 包解散，execution/execution.exit_logic 入口移除；
        # check_exit 真身单源在 trading.compute.exit，由 compute 包 re-export。
        ("trading.compute.exit", "check_exit"),
        ("trading.compute", "check_exit"),
        # check_order：双入口同源（compute / risk_shield 垫片）
        ("trading.compute.risk", "check_order"),
        ("trading.risk_shield", "check_order"),
        ("trading.compute", "check_order"),
        # build_orders_from_signals：双入口同源（compute / signal_runner 垫片）
        ("trading.compute.plan", "build_orders_from_signals"),
        ("trading.signal_runner", "build_orders_from_signals"),
        ("trading.compute", "build_orders_from_signals"),
        # stop 系列：compute / 原模块垫片
        ("trading.compute.stop", "compute_stop_price"),
        ("trading.stop_loss", "compute_stop_price"),
        ("trading.compute.stop", "check_stop_loss"),
        ("trading.order_state", "check_stop_loss"),
        ("trading.compute.stop", "check_take_profit"),
        ("trading.order_state", "check_take_profit"),
        ("trading.compute.stop", "update_trailing_stop"),
        ("trading.order_state", "update_trailing_stop"),
        # reconcile：双入口同源（compute / execution_gateway）
        ("trading.compute.reconcile", "reconcile"),
        ("trading.execution_gateway", "reconcile"),
        ("trading.compute", "reconcile"),
        # check_daily_loss_limit：双入口同源（compute / circuit_breaker 垫片）
        ("trading.compute.breaker", "check_daily_loss_limit"),
        ("trading.circuit_breaker", "check_daily_loss_limit"),
        ("trading.compute", "check_daily_loss_limit"),
        # OrderRequest：双入口同源（compute.types / execution_gateway）
        ("trading.compute.types", "OrderRequest"),
        ("trading.execution_gateway", "OrderRequest"),
        ("trading.compute", "OrderRequest"),
        # 伴随 dataclass（ExitDecision / RiskDecision / PlannedOrder / ReconciliationResult）
        ("trading.compute.exit", "ExitDecision"),
        ("trading.compute.risk", "RiskDecision"),
        ("trading.compute.plan", "PlannedOrder"),
        ("trading.signal_runner", "PlannedOrder"),
        ("trading.compute.reconcile", "ReconciliationResult"),
        ("trading.execution_gateway", "ReconciliationResult"),
        ("trading.compute.reconcile", "PositionDrift"),
        ("trading.execution_gateway", "PositionDrift"),
    ],
)
def test_all_entry_points_resolvable(module_path: str, attr: str) -> None:
    """每个入口必须可 import（垫片链路通畅性基础校验）。

    同源 is 比较由下方 test_*_same_object 系列守护——这里只验证导入不抛异常，
    避免垫片笔误（如漏写 re-export）。
    """
    mod = importlib.import_module(module_path)
    assert hasattr(mod, attr), f"{module_path}.{attr} 不可用——垫片 re-export 链断"


# —— 同源 is 断言（每个决策函数独立一组，失败时定位精确）——
def _get(module_path: str, attr: str):
    return getattr(importlib.import_module(module_path), attr)


def test_check_exit_single_source() -> None:
    a = _get("trading.compute.exit", "check_exit")
    assert a is _get("trading.compute", "check_exit")


def test_check_order_single_source() -> None:
    a = _get("trading.compute.risk", "check_order")
    assert a is _get("trading.risk_shield", "check_order")
    assert a is _get("trading.compute", "check_order")


def test_build_orders_single_source() -> None:
    a = _get("trading.compute.plan", "build_orders_from_signals")
    assert a is _get("trading.signal_runner", "build_orders_from_signals")
    assert a is _get("trading.compute", "build_orders_from_signals")


def test_compute_stop_price_single_source() -> None:
    assert (
        _get("trading.compute.stop", "compute_stop_price")
        is _get("trading.stop_loss", "compute_stop_price")
    )


def test_order_state_stops_single_source() -> None:
    for fn in ("check_stop_loss", "check_take_profit", "update_trailing_stop"):
        assert (
            _get("trading.compute.stop", fn)
            is _get("trading.order_state", fn)
        ), f"{fn} 双源"


def test_reconcile_single_source() -> None:
    a = _get("trading.compute.reconcile", "reconcile")
    assert a is _get("trading.execution_gateway", "reconcile")


def test_breaker_single_source() -> None:
    a = _get("trading.compute.breaker", "check_daily_loss_limit")
    assert a is _get("trading.circuit_breaker", "check_daily_loss_limit")


def test_order_request_single_source() -> None:
    a = _get("trading.compute.types", "OrderRequest")
    assert a is _get("trading.execution_gateway", "OrderRequest")


def test_order_state_enum_single_source() -> None:
    """OrderState 枚举单源契约（Layer2 阶段5 · 真身迁 trading/types/order_state.py）。

    三入口同源：types（新源）/ order_state（旧模块垫片 re-export）/ trading 包级导出。
    """
    a = _get("trading.types.order_state", "OrderState")
    assert a is _get("trading.order_state", "OrderState")
    assert a is _get("trading", "OrderState")


def test_cancel_all_open_orders_single_source() -> None:
    """cancel_all_open_orders 单源契约（Layer2 阶段5 · 副作用迁 trading/io/breaker.py）。

    双入口同源：io.breaker（新源·副作用壳）/ circuit_breaker（旧模块垫片 re-export）。
    纯判定 check_daily_loss_limit 仍单源在 compute.breaker（见 test_breaker_single_source）。
    """
    a = _get("trading.io.breaker", "cancel_all_open_orders")
    assert a is _get("trading.circuit_breaker", "cancel_all_open_orders")
