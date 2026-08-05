r"""SSoT 静态护栏：生产代码零 CSV/record_live_trade 代码引用（防回归）。

物理意图：CSV 镜像退役后，新代码重新引入 record_live_trade 调用 / LIVE_TRADE_LOG 赋值 /
live_trades.csv 字面量 = 重新引入第二真相源。本测试用 Python re 精确 pattern 扫【代码引用】
（调用/赋值/import/字面量），命中即 FAIL，防回归。

为何「精确 pattern」而非「字面 substring」：
  - A4 删除 record_live_trade/LIVE_TRADE_LOG/LIVE_TRADE_COLUMNS 时，按 CLAUDE.md「注释
    说明为什么」原则保留了 11 行审计追溯注释（如 engine.py:3201 「原 record_live_trade CSV
    审计块已删除」、trading_service.py:38 「LIVE_TRADE_LOG/LIVE_TRADE_COLUMNS）已整体退役」）。
  - 这些注释里只出现【名字】（无括号、无等号、无 import 关键字、无引号包裹 csv 字面），
    不构成代码引用。
  - 精确 pattern（record_live_trade\( / LIVE_TRADE_LOG\s*= / import record_live_trade 等）
    只命中真正的代码语法，审计注释合法保留不误命中。

为何用纯 Python re 而非 ripgrep/grep：
  - 跨平台确定性：Windows 上 rg 是 Claude Code shell 函数（非真实二进制，Python subprocess
    不可见），grep -E 的 \(\) 转义在 MSYS grep 上行为异常（「Unmatched (」误报）。
  - 自包含：测试不应依赖外部二进制是否安装。Python re 是零依赖、行为确定的真相源。
  - 符合 Karpathy「No Magic」：显式遍历 + re.search，可见可控。
"""
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 护栏扫描的生产目录（brief 指定）
# - trading：核心引擎 + state_store + audit
# - presentation/server：trading_service（原 CSV 写盘入口）
# - broadcast：原 CSV 读口回退路径
# - research：研究脚本可能写盘
# - scripts：运维脚本可能写盘
PROD_DIRS = ["trading", "presentation/server", "broadcast", "research", "scripts"]

# BANNED 代码引用 pattern（精确，跳注释/docstring）
# - record_live_trade\(  → 函数调用（注释里「record_live_trade 平移」无括号，不命中）
# - LIVE_TRADE_LOG\s*=    → 常量赋值/定义（注释提及无 =，不命中）
# - LIVE_TRADE_COLUMNS\s*= → 常量赋值/定义（同上）
# - os\.getenv.*LIVE_TRADE_READ_SOURCE → env 读口回退（A2 已删，回退口禁用）
# - ["']live_trades\.csv  → 字符串字面量（open/Path 用，注释里「logs/live_trades.csv」无引号不命中）
# - import record_live_trade → import 语句（A4 删函数定义后无导入目标）
# - ["']param_iter_state\.json → B3（2026-08-05）legacy 冠军治理 JSON 字符串字面量（open/Path 用，
#   注释/docstring 裸名无引号不命中；B3 切 ACTIVE 后生产代码不再 open 此文件，命中即回归）
#
# 注：ripgrep 默认不跳注释；本护栏「跳注释」语义完全来自 pattern 精确性
#     （要求括号/等号/引号/import 关键字），而非 rg 自动识别注释。
BANNED_PATTERNS = [
    r"record_live_trade\(",
    r"LIVE_TRADE_LOG\s*=",
    r"LIVE_TRADE_COLUMNS\s*=",
    r"os\.getenv.*LIVE_TRADE_READ_SOURCE",
    r"[\"']live_trades\.csv",
    r"\bimport\b.*\brecord_live_trade\b",
    r"[\"']param_iter_state\.json",
    # C3：trading_plan 写路径已删（DB trade_event(SIGNAL/CONFIRMED) 唯一真相源）。
    # 精确 pattern（防 legacy shim save_plan_legacy/confirm_plan_legacy 误命中：
    # \b 边界保证 confirm_plan 后紧跟 ( 不匹配 confirm_plan_legacy()）
    r"\bsave_plan\s*\(",
    r"\bconfirm_plan\s*\(",
    r"\bdef\s+save_plan\b",
    r"\bdef\s+confirm_plan\b",
    r"trading_plan\.save_plan\b",
    r"trading_plan\.confirm_plan\b",
]

# 预编译 pattern（性能 + 一次性校验正则合法性）
_BANNED_COMPILED = [re.compile(p) for p in BANNED_PATTERNS]


def _iter_prod_py_files(targets: list[str]):
    """遍历 targets 下所有 .py 文件，排除 archive/ 与 tests/。

    yield 绝对 Path。targets 为相对 ROOT 的目录列表。
    """
    for t in targets:
        base = ROOT / t
        if not base.exists():
            continue
        for py in base.rglob("*.py"):
            # 路径标准化为正斜杠做包含判断（兼容 win 反斜杠）
            s = str(py).replace("\\", "/")
            if "/archive/" in s or "/tests/" in s:
                continue
            # C3：排除 BANNED 防御者自身（pattern 字面字符串会被同 pattern 命中）
            if py.name == "audit_ssot.py" and "scripts" in s:
                continue
            if py.name == "test_ssot_static_guard.py":
                continue
            yield py


def _scan(patterns: list[str], targets: list[str]) -> list[str]:
    """用 Python re 扫 targets 下 .py 文件，返回命中行（带 file:line:content 格式）。

    排除 archive/ 与 tests/。每个 pattern 独立扫，命中行去重保序。
    """
    compiled = [re.compile(p) for p in patterns]
    hits = []
    seen = set()  # (file, lineno) 去重（多 pattern 命中同行只报一次）
    for py in _iter_prod_py_files(targets):
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            key = (str(py), i)
            if key in seen:
                continue
            for rx in compiled:
                if rx.search(line):
                    hits.append(f"{py}:{i}:{line}")
                    seen.add(key)
                    break  # 同行多 pattern 命中只记一次
    return hits


def _rg(pattern: str) -> list[str]:
    """兼容旧接口：扫生产目录命中行（排除 archive/ 与 tests/）。

    内部走 _scan 单 pattern。保留此函数名以匹配 brief 的代码示例语义。
    """
    return _scan([pattern], PROD_DIRS)


def test_no_live_trades_code_reference():
    """生产代码零 CSV/record_live_trade 代码引用（精确 pattern，跳注释）。

    命中即 FAIL：A1-A4 已删除所有代码引用，新代码若重新引入即第二真相源回归。
    """
    hits = _scan(BANNED_PATTERNS, PROD_DIRS)
    assert not hits, (
        "SSoT 护栏命中代码引用（注释/docstring 不计）：\n" + "\n".join(hits)
    )


def test_no_disk_live_trades_csv():
    """logs/live_trades.csv 不存在（仅 logs/archive/ 允许）。

    物理意图：CSV 镜像退役后，根 logs/ 下不应再有 live_trades.csv（应归档到 logs/archive/）。
    若存在 = 上次 run 又写了 CSV = SSoT 被破坏。
    """
    assert not (ROOT / "logs" / "live_trades.csv").exists(), (
        "logs/live_trades.csv 仍存在（应归档到 logs/archive/）"
    )


def test_no_disk_param_iter_state_json():
    """logs/param_iter_state.json 不存在（B3 收口，仅 logs/archive/ 允许）。

    物理意图：legacy 冠军治理 JSON 已被 experiment.db ACTIVE 取代（单一真相源）。
    根 logs/ 下不应再有此文件（应归档到 logs/archive/）。若存在 = 上次又有进程写了
    legacy JSON = 双轨未消除。param_iter 入口已 fail-closed（不带 --legacy 拒绝运行），
    生产链路无写入路径——若文件重生说明 param_iter 又被外部脚本拉起。
    """
    assert not (ROOT / "logs" / "param_iter_state.json").exists(), (
        "logs/param_iter_state.json 仍存在（B3 已收口，应归档到 logs/archive/）；"
        "若文件重生说明 param_iter 又被外部脚本拉起，需停调度"
    )


def test_guard_pattern_distinguishes_code_vs_comment():
    """护栏敏感性自检：证明精确 pattern 区分代码引用 vs 审计注释。

    构造临时 .py 文件，分别注入：
      (a) 审计追溯注释（裸名无括号）：`# 原 record_live_trade CSV 审计块已删除`
          —— 模拟 A4 保留的 11 行注释。期望【不命中】（无括号 = 无代码语法）
      (b) 代码引用（函数调用）：`_ = record_live_trade(1)`
          —— 模拟新代码回归。期望【命中】（有括号 = 代码语法）
    断言 (a) 不命中、(b) 命中。证明 pattern 精确性是护栏有效的根因。

    反向证明护栏不是 rubber-stamp：若有人误以为「re 自动跳注释」而写带括号的注释
    `# record_live_trade(` 仍会被命中（pattern 是字面精确匹配，不依赖工具跳注释）。
    """
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "_probe.py"
        # (a) 审计注释（裸名，无括号/等号/import/引号）—— A4 风格，应不命中
        # (b) 代码引用（函数调用）—— 回归，应命中
        probe.write_text(
            "# probe file for guard sensitivity\n"
            "# (a) 审计注释：原 record_live_trade CSV 审计块已删除\n"
            "#     LIVE_TRADE_LOG/LIVE_TRADE_COLUMNS）已整体退役\n"
            "# (b) 代码引用（回归）：\n"
            "_ = record_live_trade(1)\n",
            encoding="utf-8",
        )
        # 在临时文件单独跑 pattern（targets=[临时目录绝对路径]，不走 PROD_DIRS）
        hits = _scan([r"record_live_trade\("], [str(probe.parent)])

    # 期望仅 1 行命中：代码引用 `record_live_trade(1)`
    # 审计注释行（a）虽含 `record_live_trade` 字面，但无 `(` → 不命中
    assert len(hits) == 1, (
        f"护栏敏感性异常：期望仅 1 行代码引用命中，实际 {len(hits)} 行：\n{hits}"
    )
    assert "record_live_trade(1)" in hits[0], (
        f"命中行非预期代码引用：{hits[0]}"
    )
