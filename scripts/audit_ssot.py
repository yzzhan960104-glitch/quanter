r"""SSoT 一致性巡检（B6）。

物理意图：单一真相源（SSoT）硬化后，定期校验 DB 内部一致性 + 生产代码零回归 +
引擎单例。任一 check 命中即退出码 1（CI / 运维可挂); 全绿退出码 0。

退出码语义：
  0 = 全绿（DB 一致 + 引擎单例 + 生产代码零 BANNED 引用）
  1 = 有不一致（具体错误行打印到 stdout）

设计原则（CLAUDE.md「显式至上 + 量化风控拷问」）：
  - 精确 SQL 检查，绝不 `return None` 占位（每个 check 真实跑 SQL/进程/正则扫描）。
  - float 容差 1e-6（fill.qty 是 REAL，浮点累加误差容忍）。
  - 孤儿 SIGNAL 判 >7 日（生产合法未确认 SIGNAL <7 日不告警，只炸真正断链）。
  - 跨平台：Windows wmic / 非 Windows pgrep（C-5 单例约束）。
  - 护栏复用 A5 静态护栏的精确 pattern 哲学（跳注释：要求括号/等号/引号/import 关键字）。

BANNED pattern 边界（B6 决策 / C3 收尾）：
  - live_trades.csv / record_live_trade / LIVE_TRADE_LOG / LIVE_TRADE_COLUMNS /
    LIVE_TRADE_READ_SOURCE —— Phase A 已删，命中即第二真相源回归。
  - param_iter_state.json —— B3 已收口（experiment.db ACTIVE 唯一真相源），命中即 legacy 回归。
  - trading_plan.save_plan / trading_plan.confirm_plan —— C3 已删（DB trade_event(SIGNAL/
    CONFIRMED) 唯一真相源）。tests/_legacy_plan_io.py 测试专用 legacy shim 例外（PROD_DIRS
    不扫 tests/，不会误命中）。
"""
import platform
import re
import subprocess
import sqlite3
import sys
from pathlib import Path

# Windows GBK 控制台无法编码 ↔/✓ 等 Unicode（脚本含中文 + 符号）。
# 强制 stdout/stderr 走 utf-8，避免 UnicodeEncodeError 中断巡检。
# 物理意图：运维可能在 cron / schtasks / 手动 cmd 里跑本脚本，编码崩溃 = 巡检失效。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    # reconfigure 在非 TextIOWrapper 或老 Python 不可用 —— 回退忽略非编码字节
    pass

ROOT = Path(__file__).resolve().parents[1]
# code-review 修复：进程拓扑探测与 trading_supervisor 共用 ops/process_topology.py
# （原实现复制两份 → 端口/pid 文件/引擎进程口径漂移，如 audit 版漏了 LOCK_DIR env）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from ops.process_topology import (
    client_status as _client_status,
    default_port as _default_port,
    engine_processes as _engine_processes,
    pid_file_owner as _pid_file_owner,
    port_holder_pid as _port_holder_pid,
)
DB_PATH = ROOT / "logs" / "trading_state.db"

# 护栏扫描的生产目录（与 tests/test_ssot_static_guard.py PROD_DIRS 同口径）
# - trading：核心引擎 + state_store
# - presentation/server：trading_service（原 CSV 写盘入口）
# - broadcast：原 CSV 读口回退路径
# - research：研究脚本可能写盘
# - scripts：运维脚本可能写盘（含本文件自身，但本文件不写 BANNED 字面量）
PROD_DIRS = ["trading", "presentation/server", "broadcast", "research", "scripts"]

# BANNED 代码引用 pattern（精确，跳注释）
# 物理意图：A4 删除时按 CLAUDE.md「注释说明为什么」保留 11 行审计追溯注释
# （如「原 record_live_trade CSV 审计块已删除」），注释里只出现【名字】（无括号/
# 等号/import/引号/csv 字面），不构成代码引用。精确 pattern 要求真正的代码语法，
# 注释合法保留不误命中。与 tests/test_ssot_static_guard.py BANNED_PATTERNS 同口径。
BANNED_PATTERNS = [
    r"record_live_trade\(",          # 函数调用（注释「record_live_trade 平移」无括号不命中）
    r"LIVE_TRADE_LOG\s*=",           # 常量赋值/定义
    r"LIVE_TRADE_COLUMNS\s*=",       # 常量赋值/定义
    r"os\.getenv.*LIVE_TRADE_READ_SOURCE",  # env 读口回退（A2 已删）
    r"[\"']live_trades\.csv",        # 字符串字面量（注释「logs/live_trades.csv」无引号不命中）
    r"\bimport\b.*\brecord_live_trade\b",   # import 语句
    r"[\"']param_iter_state\.json",  # B3 legacy 冠军治理 JSON 字符串字面量
    # C3：trading_plan 写路径已删（DB trade_event(SIGNAL/CONFIRMED) 唯一真相源）。
    # 精确 pattern（函数调用 + 模块属性访问 + def 定义）：
    r"\bsave_plan\s*\(",             # save_plan( 调用（不含 legacy shim 的 save_plan_legacy）
    r"\bconfirm_plan\s*\(",          # confirm_plan( 调用（不含 legacy shim 的 confirm_plan_legacy）
    r"\bdef\s+save_plan\b",          # def save_plan 函数定义（防 trading_plan.py 内回写）
    r"\bdef\s+confirm_plan\b",       # def confirm_plan 函数定义
    r"trading_plan\.save_plan\b",    # 模块属性访问（trading_plan.save_plan）
    r"trading_plan\.confirm_plan\b", # 模块属性访问（trading_plan.confirm_plan）
]
_BANNED_COMPILED = [re.compile(p) for p in BANNED_PATTERNS]


def check_fill_position(db: Path):
    """fill 流水 ↔ position 持仓一致性校验。

    物理意图：position.qty 是「成交累加 - 卖出」的快照；fill 表是成交真相源
    （UNIQUE(order_id, traded_time) 幂等）。若两者不平，说明 position 写入路径
    漏了 fill，或 fill 重复落库（幂等失效）—— 任意一种都是 SSoT 黑洞。

    算法：对每 symbol，fill 表 SUM(CASE WHEN direction='BUY' THEN +qty ELSE -qty END)
    应等于 position.qty（float 容差 1e-6 应对 REAL 累加误差）。
    """
    if not db.exists():
        return f"DB 不存在: {db}"
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    # 显式累加（清晰平铺，避免 SUM(CASE...) 在空表/无方向字段时返回 NULL 的陷阱）
    fills = {}
    for r in con.execute("SELECT symbol, direction, qty FROM fill"):
        delta = r["qty"] if r["direction"] == "BUY" else -r["qty"]
        fills[r["symbol"]] = fills.get(r["symbol"], 0) + delta
    mismatches = []
    for r in con.execute("SELECT symbol, qty FROM position WHERE qty != 0"):
        expected = fills.get(r["symbol"], 0)
        if abs(expected - r["qty"]) > 1e-6:
            mismatches.append(
                f"{r['symbol']}: fill累加={expected} vs position.qty={r['qty']}"
            )
    con.close()
    return ("fill↔position 不一致 " + "; ".join(mismatches)) if mismatches else None


def check_account_daily_closed(db: Path):
    """account_daily 每交易日 start+close 非空（熔断基线闭合）。

    物理意图：account_daily 是熔断基线真相源（C-1 熔断读口断链修复后的唯一口径）。
    熔断判定需 start_total_asset（日内 -3% 基线）+ close_total_asset（日终校验）。
    任一为 NULL = 当日熔断裸奔 / 日终未闭合 = SSoT 完整性破坏。
    """
    if not db.exists():
        return f"DB 不存在: {db}"
    con = sqlite3.connect(db)
    # 任一为 NULL 即炸（OR 而非 AND —— 闭合要求两端都落）
    rows = con.execute(
        "SELECT account_id, date FROM account_daily "
        "WHERE start_total_asset IS NULL OR close_total_asset IS NULL"
    ).fetchall()
    con.close()
    if not rows:
        return None
    missing = [f"{r[1]}/{r[0]}" for r in rows]  # date/account_id
    return f"account_daily 缺 start/close（熔断基线未闭合）: {missing}"


def check_trade_event_chain(db: Path):
    """trade_event 生命周期链完整性：孤儿 SIGNAL（>7 日无后续）告警。

    物理意图：SIGNAL 是计划源（Phase C 升格 meta 为真相源），后续应有
    CONFIRMED/VETOED/OPEN/FILLED/CLOSED 之一标记链路推进。<7 日合法未确认
    （研究员可能延后处理）；>7 日无后续 = 链路断（信号丢失 / 漏 confirm）。
    """
    if not db.exists():
        return f"DB 不存在: {db}"
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    # NOT EXISTS 子查询：该 trade_id 无任何后续状态行
    # datetime('now','-7 days') 走 SQLite 原生时间算术（UTC；timestamp 是 ISO8601 本地）
    orphans = con.execute(
        "SELECT trade_id, symbol, timestamp FROM trade_event e1 "
        "WHERE e1.action='SIGNAL' "
        "AND NOT EXISTS (SELECT 1 FROM trade_event e2 "
        "                WHERE e2.trade_id=e1.trade_id "
        "                AND e2.action IN ('CONFIRMED','VETOED','OPEN','FILLED','CLOSED')) "
        "AND e1.timestamp < datetime('now','-7 days')"
    ).fetchall()
    con.close()
    if not orphans:
        return None
    items = [f"{r['symbol']}@{r['timestamp'][:10]}" for r in orphans]
    return f"孤儿 SIGNAL（>7 日无后续 CONFIRMED/VETOED/OPEN/FILLED/CLOSED）: {items}"


def check_engine_process_count():
    """引擎进程数 ≤ 1（C-5 单例：__main__ run_server 端口 8000 单例）。

    物理意图：C-5 已将引擎收编为 uvicorn 单进程（端口 8000 单例，run_server 入口）。
    若出现 ≥2 个 `python -m trading` 进程 = 双进程抢 QMT session（[[qmt-connect-1-rootcause]]
    事故重演）/ 端口冲突 / session 漂移。

    跨平台实现（A6）：
      - Windows: PowerShell Get-CimInstance 取 commandline（弃 wmic——新 Windows 已
        deprecated 且 08-06 实测 RPC 失败/超时），失败降级为空（宁可漏报不假报）
      - 非 Windows: pgrep -f "python.*-m trading"
    """
    if platform.system() != "Windows":
        result = subprocess.run(
            ["pgrep", "-f", "python.*-m trading"],
            capture_output=True, text=True, check=False,
        )
        n = len([line for line in result.stdout.splitlines() if line.strip()])
    else:
        n = len(_engine_processes())
    return f"引擎进程数 {n} > 1（C-5 单例红线，端口 8000 / QMT session 抢占）" if n > 1 else None


def check_client_process():
    """miniQMT 客户端进程数 == 1（A6；0=未起，>1=多实例，None=探测失败）。"""
    st = _client_status()
    if st.get("count") is None:
        return "miniQMT 客户端进程探测失败（PowerShell 不可用/超时）"
    if st["count"] != 1:
        return f"miniQMT 客户端进程数 {st['count']} != 1（应恰好一个 XtMiniQmt.exe）"
    return None


def check_port_owner_consistency(port: int | None = None):
    """端口属主 == pid 文件 PID（A6；不一致 = 旧链/非法链，与 supervisor 三合一同口径）。"""
    port = _default_port() if port is None else port
    owner = _port_holder_pid(port)
    pidf = _pid_file_owner()
    if owner is not None and pidf is not None and owner != pidf:
        return f"端口 {port} 属主 {owner} != pid 文件 {pidf}"
    if owner is not None and pidf is None:
        return f"端口 {port} 被 {owner} 占用但无 pid 文件（旧链/非法链）"
    if owner is None and pidf is not None:
        return f"pid 文件存在（{pidf}）但端口 {port} 无监听（进程已死/未绑定）"
    return None


def _iter_prod_py_files(targets):
    """遍历 targets 下所有 .py 文件，排除 archive/ 与 tests/ + 本巡检自身。yield 绝对 Path。

    C3：排除 scripts/audit_ssot.py 自身——BANNED_PATTERNS 的 pattern 字面字符串（如
    ``r"\\bsave_plan\\s*\\("``）会被同文件 pattern 命中（防御者不能扫自己）。
    """
    for t in targets:
        base = ROOT / t
        if not base.exists():
            continue
        for py in base.rglob("*.py"):
            s = str(py).replace("\\", "/")
            if "/archive/" in s or "/tests/" in s:
                continue
            # 排除本巡检（防御者）+ 静态守卫测试（同款 pattern 字面）
            if py.name == "audit_ssot.py" and "scripts" in s:
                continue
            if py.name == "test_ssot_static_guard.py":
                continue
            yield py


def check_guard_ripgrep():
    """护栏复用 A5：生产代码零 BANNED 引用（精确 pattern，跳注释）。

    物理意图：与 tests/test_ssot_static_guard.py 同口径 —— 用 Python re 精确 pattern
    扫生产目录 .py 文件，命中即第二真相源回归。本函数是该测试的运维侧镜像：
    测试在 CI/pytest 时炸，本巡检在运维/调度时炸（双保险）。

    为何不 import test_ssot_static_guard：测试模块位于 tests/，本脚本位于 scripts/，
    跨目录 import 不健壮（依赖 cwd / sys.path）；pattern 同口径复制（DRY 让位于
    部署隔离）。pattern 漂移由 tests/test_ssot_static_guard.py 的同源注释约束。
    """
    hits = []
    seen = set()  # (file, lineno) 去重（多 pattern 命中同行只报一次）
    for py in _iter_prod_py_files(PROD_DIRS):
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            key = (str(py), i)
            if key in seen:
                continue
            for rx in _BANNED_COMPILED:
                if rx.search(line):
                    hits.append(f"{py}:{i}:{line}")
                    seen.add(key)
                    break  # 同行多 pattern 命中只记一次
    return ("SSoT 护栏命中 BANNED 代码引用（注释不计）:\n" + "\n".join(hits)) if hits else None


def main():
    """跑所有 check，errs 非空 sys.exit(1) 否则 print 全绿 + sys.exit(0)。"""
    db = DB_PATH
    checks_db = [
        ("fill↔position", check_fill_position),
        ("account_daily 闭合", check_account_daily_closed),
        ("trade_event 链", check_trade_event_chain),
    ]
    checks_runtime = [
        ("引擎进程数", check_engine_process_count),
        ("miniQMT 客户端进程", check_client_process),
        ("端口属主一致性", check_port_owner_consistency),
        ("护栏 BANNED", check_guard_ripgrep),
    ]

    errs = []
    print("=== SSoT 一致性巡检（audit_ssot.py）===")
    print(f"DB: {db}")
    for name, fn in checks_db:
        err = fn(db)
        status = "PASS" if err is None else "FAIL"
        print(f"  [{status}] {name}")
        if err:
            errs.append(f"[{name}] {err}")
    for name, fn in checks_runtime:
        err = fn()
        status = "PASS" if err is None else "FAIL"
        print(f"  [{status}] {name}")
        if err:
            errs.append(f"[{name}] {err}")

    if errs:
        print("\n=== 不一致（%d 项）===" % len(errs))
        for e in errs:
            print(" -", e)
        sys.exit(1)
    print("\naudit_ssot: 全绿（DB 一致 + 引擎单例 + 护栏零回归）")
    sys.exit(0)


if __name__ == "__main__":
    main()
