# C-5 进程模型统一 + 网关健康前置 gate 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一 `python -m trading` 入口到 uvicorn（消除双进程抢 QMT session），并抽 `_gw_health_gate` 共享前置让 `_stoploss`/`_post_close` 在网关锁态时 skip+CRITICAL 不跑业务。

**Architecture:** (C) `trading/__main__.py` 改 uvicorn 薄壳、废弃 `_run_forever`，端口 8000 天然单例替代文件锁；`presentation/server/main.py` lifespan 装配 engine 前调 `log_startup_banner`（生产链 session 漂移可见）。(B) `trading/engine.py` 抽 `_gw_health_gate`（从 `_pre_open_gate` ② 段），`_pre_open_gate` DRY 改调它；`_stoploss`/`_post_close` 在 `@_critical_guard` 后、交易日守卫前调 gate，锁态 skip+CRITICAL 不停调度（与 `_pre_open_gate` + `_health_guard` 自愈取向一致）。

**Tech Stack:** Python 3.10、FastAPI/uvicorn、APScheduler、pytest（`asyncio.run(...)` 同步驱动范式，仓库未配 pytest-asyncio mode）。

## Global Constraints

- **全中文注释**（CLAUDE.md 协议）：所有新增/修改代码块配像素级中文注释，说明 What + Why（交易物理意图）。
- **TDD 纪律**：每步先写失败测试 → 跑红 → 最小实现 → 跑绿 → commit，不许「先实现后补测试」。
- **不开 `SO_REUSEPORT`**（spec §3.3 R6）：uvicorn 默认 False，本期显式不传 `reuse_port`，端口 8000 bind 失败即 exit（天然单例）。
- **不加文件锁/PID 锁**（spec §2 非目标）：端口单例已够，A 方案废弃。
- **不改 `_health_guard` 不升 L1 决议**（spec §2 非目标，C-4 已定）：B gate skip 与之同取向（自愈不停调度）。
- **不抽 gate 装饰器**（spec §2 非目标）：三入口（`_pre_open_gate`/`_stoploss`/`_post_close`）显式调 `_gw_health_gate` 共享方法，不引入新抽象层。
- **不改 schtasks 命令字面**（spec §3.4）：`python -m trading` 字面不变，内部从「独立 engine」变「起 uvicorn」。
- **全量回归基线**：C-4 merged 后 **1146 passed / 0 failed**（spec §7 验收 7），本期零退化。
- **测试入口**：Windows 环境用 `F:/quanter/.venv310/Scripts/python.exe -m pytest ...`（下文简写为 `pytest ...`）。
- **测试范式**：新测试用 `asyncio.run(eng.method())` 同步驱动（与 `test_engine_pre_open_gate.py` / `test_engine_stoploss_inject.py` 一致），不引入 `@pytest.mark.asyncio` 风格分叉。
- **commit 规范**：`feat(c5-vN): ...` / `fix(c5-vN): ...`，与 C-4 的 `c4-uN` 同构。

## File Structure

| 文件 | 责任 | 本期改动 |
|---|---|---|
| `trading/__main__.py` | 开发/调试常驻入口 | **V1**：新增 `run_server()` 薄函数起 uvicorn；废弃 `_run_forever`；`if __name__` 改调 `run_server()`；不再 `sys.exit` shadow_gate（lifespan 处理）。保留 `check_shadow_gate`/`_days_since_activation`/`log_startup_banner`（lifespan 用）。更新 docstring。 |
| `presentation/server/main.py` | FastAPI app + lifespan | **V2**：lifespan 装配 TradingEngine 前（line 191 try 块内、`TradingEngine()` 前）调 `log_startup_banner()`；import 行补 `log_startup_banner`。 |
| `trading/engine.py` | TradingEngine（四触发点 + gate） | **V3**：新增 `_gw_health_gate(self, gw)` 方法；`_pre_open_gate` ② 段 DRY 改调它。**V4**：`_stoploss`/`_post_close` 在 `@_critical_guard` 后、交易日守卫前调 `_gw_health_gate`，锁态 `_alert_critical` + return。 |
| `tests/trading/test_main.py` | `__main__` 入口契约锁 | **V1**：删 `_run_forever` 相关 3 个测试；新增 `run_server` 契约锁（uvicorn 调用参数 + live 不 reload + 顶层无裸 asyncio.run）。保留 `test_module_importable`。 |
| `tests/trading/test_main_banner.py` | `log_startup_banner` 单测 | **V2**：新增 `test_lifespan_calls_log_startup_banner`（源码级断言 main.py lifespan 调 banner）。 |
| `tests/trading/test_gw_health_gate.py` | `_gw_health_gate` 单测（新建） | **V3**：4 用例（gw None / 未连接 / 客户端未就绪 / 全绿）。 |
| `tests/trading/test_engine_pre_open_gate.py` | `_pre_open_gate` 三段 gate 回归 | **V3**：无需改（② 段 DRY 后行为不变，既有 9 用例即回归）。 |
| `tests/trading/test_stoploss_post_close_gate.py` | `_stoploss`/`_post_close` gate（新建） | **V4**：4 用例（两 job 各自 gw 锁态 skip+alert+不跑业务 / gw 绿放行）。 |
| `tests/trading/test_engine_stoploss_inject.py` | `_stoploss` stop_prices 注入 | **V4**：3 个既有用例补 `patch get_gateway` 返 connected+ready gw（让 gate 放行才能测下游注入），否则 gate skip 到不了 stop_loss_monitor。 |
| `scripts/start_all.bat` / `ops/start_all.py` / `ops/manage_ops_schtasks.py` | 生产启动/schtasks | **V5**：不改代码（已确认 start_all.py:89-90 生产链已用 uvicorn）；V5 做源码级命令字面断言 + 全量回归。 |

---

## Task 1 (V1)：`__main__` 改 uvicorn 薄壳 + 废弃 `_run_forever`

**Files:**
- Modify: `trading/__main__.py`（废弃 `_run_forever` line 199-234；新增 `run_server()`；改 `if __name__` 块 line 237-261；更新模块 docstring line 1-54）
- Test: `tests/trading/test_main.py`（删 3 个 `_run_forever` 契约锁，新增 `run_server` 契约锁）

**Interfaces:**
- Consumes: `presentation.server.main:app`（uvicorn 加载的 ASGI app，已存在）；`os.getenv("SERVER_HOST"/"SERVER_PORT"/"AUTO_TRADE_MODE")`（顶部 `load_dotenv(override=True)` 已加载 .env）。
- Produces: `trading.__main__.run_server() -> None`（薄函数，起 uvicorn；测试 mock `uvicorn.run` 断言参数）。`check_shadow_gate`/`_days_since_activation`/`log_startup_banner` 签名不变（lifespan 依赖）。

- [ ] **Step 1：先 grep 确认 `_run_forever` 无外部调用方**（除 test_main.py 自身）

Run: `grep -rn "_run_forever" F:/quanter --include="*.py" | grep -v "trading/__main__.py"`
Expected: 仅命中 `tests/trading/test_main.py`（3 处：`test_run_forever_is_callable` / `test_asyncio_run_is_main_guarded` / `test_main_calls_bootstrap_before_start` 的 docstring/断言）。无其他模块 import `_run_forever`，废弃安全。若命中 lifespan/其他模块，**STOP** 评估调用方。

- [ ] **Step 2：写失败测试（重写 test_main.py）**

整文件覆盖 `tests/trading/test_main.py` 为：

```python
# -*- coding: utf-8 -*-
"""``trading.__main__`` 入口契约锁（C-5 V1：uvicorn 薄壳改造后）。

测试边界（plan 未要求 __main__ 全量单测，本测试为契约锁）：
- 锁住「入口模块可 import」契约：import trading.__main__ 不崩、不阻塞（uvicorn.run
  在 if __name__ 守卫内，import 不起 server）。
- 锁住「run_server 是可调用对象」契约（V1：取代废弃的 _run_forever）。
- 锁住「run_server 起 uvicorn :8000，live 模式 reload=False」契约（spec §3.1/§3.3）。
- 锁住「_run_forever 已废弃」契约（V1：源码不再含 async def _run_forever 定义）。

物理意图（C-5 V1）：
    __main__ 从「独立起 engine 的 _run_forever」改造为「起 uvicorn 让 lifespan 装
    engine」，消除双进程抢 QMT_SESSION_ID（07-29 故障根因）。uvicorn bind 8000 天然
    单例（第二实例 WSAEADDRINUSE exit），无需文件锁。
"""
from __future__ import annotations

import trading.__main__ as main_mod


def test_module_importable():
    """入口模块可 import 不崩（锁住「入口可 import」契约）。"""
    assert main_mod is not None
    assert main_mod.logger is not None


def test_run_forever_removed():
    """V1：_run_forever 已废弃（源码不再定义 async def _run_forever）。

    物理意图：_run_forever 独立装配 engine 是双进程抢 session 的根因，V1 废弃后
    engine 只由 uvicorn lifespan 装配。若未来误恢复 _run_forever，本测试即红。
    """
    import pathlib
    src = pathlib.Path(main_mod.__file__).read_text(encoding="utf-8")
    assert "async def _run_forever" not in src, (
        "_run_forever 已在 C-5 V1 废弃（engine 装配收归 uvicorn lifespan），"
        "源码不应再定义 async def _run_forever")


def test_run_server_is_callable():
    """V1：run_server 是可调用对象（取代 _run_forever 的入口契约）。"""
    assert callable(main_mod.run_server)


def test_run_server_calls_uvicorn_port_8000(monkeypatch):
    """V1：run_server 起 uvicorn :8000（spec §3.1/§3.3 端口单例基线）。

    mock uvicorn.run 捕获调用参数，断言 app 指向 lifespan、port=8000、host 非空。
    不真起 server（uvicorn.run 被 lambda 替换）。
    """
    captured = {}

    def _fake_run(app, **kw):
        captured["app"] = app
        captured.update(kw)

    monkeypatch.setattr("uvicorn.run", _fake_run)
    monkeypatch.setenv("SERVER_PORT", "8000")
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    main_mod.run_server()
    assert captured["app"] == "presentation.server.main:app"
    assert captured["port"] == 8000
    assert captured["host"]  # 非空


def test_run_server_live_no_reload(monkeypatch):
    """V1 R6：live 模式 reload=False（reload 起 reloader 子进程抢 session，自扰）。

    spec §3.1 物理意图：reload 模式 uvicorn 会 fork reloader 子进程，子进程再次
    gw.connect() 抢同一 QMT_SESSION_ID → 自扰性断线。live 显式 reload=False。
    """
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    main_mod.run_server()
    assert captured.get("reload") is False, "live 模式必须 reload=False（防子进程抢 session）"


def test_run_server_dry_run_reload_true(monkeypatch):
    """V1：dry_run 模式 reload=True（开发热重载便利，无真网关无抢 session 风险）。"""
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    main_mod.run_server()
    assert captured.get("reload") is True


def test_no_asyncio_run_at_top_level():
    """V1：模块顶层无裸 asyncio.run（import 不阻塞红线）。

    _run_forever 废弃后，__main__ 不再 asyncio.run。若未来误在模块顶层（if __name__
    守卫外）调 asyncio.run / uvicorn.run，import 会阻塞。本测试 AST 级断言：顶层
    非守卫位置不得出现 asyncio.run 或 uvicorn.run 调用。
    """
    import ast
    import pathlib

    src = pathlib.Path(main_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        # 函数体内的调用不算（run_server 内部 import uvicorn 合法）
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        # if __name__ == "__main__" 守卫块内的调用合法
        if isinstance(node, ast.If):
            continue
        # 其它顶层节点（import/赋值/裸表达式）不得是 asyncio.run / uvicorn.run 调用
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.attr == "run" and func.value.id in ("asyncio", "uvicorn"):
                    raise AssertionError(
                        f"{func.value.id}.run() 不应在模块顶层调用（必须在 "
                        "run_server() 内或 if __name__ 守卫内，否则 import 阻塞）")
```

- [ ] **Step 3：跑测试验证失败**

Run: `pytest tests/trading/test_main.py -v`
Expected: `test_run_forever_removed` / `test_run_server_is_callable` / `test_run_server_calls_uvicorn_port_8000` / `test_run_server_live_no_reload` / `test_run_server_dry_run_reload_true` 全 FAIL（`run_server` 不存在、`async def _run_forever` 仍存在）；`test_module_importable` / `test_no_asyncio_run_at_top_level` 可能仍 PASS。

- [ ] **Step 4：实现——新增 `run_server()`、废弃 `_run_forever`、改 `if __name__`、更新 docstring**

**4a. 在 `trading/__main__.py` 删除整个 `_run_forever` 函数**（line 199-234，从 `async def _run_forever() -> None:` 的 def 行到其 docstring 末尾 `eng.shutdown()` 那行）。用 Edit 删除：

old_string（`_run_forever` 整个函数，从 def 上一行空行到函数体最后一行）：
```python
async def _run_forever() -> None:
    """起 TradingEngine + 守护 event loop（APScheduler 后台跑四 cron）。
```
（定位锚点——执行者按 `_run_forever` 整个函数体删除，直到 `if __name__ == "__main__":` 之前。删除后用下面的 `run_server` 替代。）

**4b. 在原 `_run_forever` 位置（`log_startup_banner` 函数之后、`if __name__` 之前）新增 `run_server()`**：

```python
def run_server() -> None:
    """C-5 V1：起 uvicorn 托管 engine（消除双进程抢 QMT session）。

    物理意图（spec §3.1 · [[qmt-connect-1-rootcause]] 教训）：
        原 ``_run_forever`` 独立装配 TradingEngine + APScheduler，与生产链
        ``start_all.py → detach uvicorn → main.py lifespan`` 形成两条并存入口。
        两进程 ``gw.connect()`` 抢同一 ``QMT_SESSION_ID`` → QMT 返回 -1（session
        占用）→ 07-29 全天锁死。本函数让 ``python -m trading`` 也走 uvicorn，
        engine 只由 lifespan 装配一次；uvicorn bind 8000 天然单例（第二实例
        ``WSAEADDRINUSE`` → uvicorn exit → 不到 lifespan → 天然不双进程），
        无需文件锁/PID 锁（spec §3.3，A 方案废弃）。

    Why live 不 reload（spec §3.1 R6）：
        ``reload=True`` 时 uvicorn 起 reloader 子进程，子进程会再次 import main →
        lifespan → gw.connect() 抢同一 session（自扰性断线）。live 模式显式
        ``reload=False``；dry_run 开 reload 便利开发热重载（无真网关无抢 session 风险）。

    Why 不再 ``sys.exit`` shadow_gate：
        V1 前独立进程模式下 ``if not check_shadow_gate(): sys.exit(2)`` 是进程级
        决策；V1 后 shadow_gate 检查完全收归 lifespan（main.py:196
        ``if check_shadow_gate(): eng.start()``），server 起不起与 engine 起不起
        解耦——影子期不足时 server 仍起（手动 API 可用），只是不 start scheduler。

    schtasks 兼容（spec §3.4）：
        ``python -m trading`` 命令字面不变（schtasks/PM2/历史注册保持），内部从
        「独立 engine」变「起 uvicorn」——schtasks 触发后起 server，engine 由
        lifespan 装。
    """
    # 惰性 import：避免模块顶层 import uvicorn 拉起 fastapi/starlette 重链
    # （test_main.py 的 import trading.__main__ 不应连带加载 server 栈）。
    import uvicorn
    uvicorn.run(
        "presentation.server.main:app",
        host=os.getenv("SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("SERVER_PORT", "8000")),
        # 显式不传 reuse_port（spec §3.3 R6）：uvicorn 默认 SO_REUSEPORT=False，
        # 第二实例 bind 8000 即 WSAEADDRINUSE exit（端口单例防护）。
        reload=(os.getenv("AUTO_TRADE_MODE", "dry_run") != "live"),
    )
```

**4c. 改 `if __name__ == "__main__":` 块**（line 237-261）：

old_string：
```python
if __name__ == "__main__":
    # 启动期模式读取（默认 dry_run · 影子红线）。
    # 缺省 dry_run：未显式 AUTO_TRADE_MODE=live 一律按影子处理，宁可漏挂单也
    # 不在未观测足够天数时盲发真单。
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    logger.info("=== 自动交易引擎启动（AUTO_TRADE_MODE=%s）===", mode)

    # Plan 4 T6：≥5 天影子期硬闸（fail-closed 真·闸）。
    # 取代原启动期 WARNING 段（spec §5.3 误称"硬闸"实为提醒——切 LIVE 只需改 env，
    # 无任何拦单）。check_shadow_gate 内部 dry_run 直接放行；live 时查所有 ACTIVE 实验
    # activated_at，任一影子期 < TRADE_SHADOW_MIN_DAYS → 返 False + 钉钉 CRITICAL。
    # 异常 fail-closed / 空列表放行 / activated_at 缺失保守拒绝（D3/D8/D9）。
    # W2（C-2 Task 5）：check_shadow_gate 返 bool 而非 sys.exit——独立进程模式下
    # 拒切 LIVE 仍 sys.exit(2)（进程级决策）；engine 合并进 uvicorn 后由 server
    # lifespan 据 bool 决定是否起 engine，不再 sys.exit 杀掉整个 API server。
    if not check_shadow_gate():
        logger.error("影子期不足，拒绝启动 engine（独立进程模式退出）")
        sys.exit(2)   # 独立进程模式仍可 exit；uvicorn 模式由 lifespan 决定

    try:
        asyncio.run(_run_forever())
    except KeyboardInterrupt:
        # Ctrl-C 在 asyncio.run 外层再次被捕（双保险）。
        logger.info("收到 Ctrl-C，进程退出。")
        sys.exit(0)
```

new_string：
```python
if __name__ == "__main__":
    # C-5 V1：起 uvicorn 托管 engine（替代独立 _run_forever 进程）。
    # mode 仅用于启动日志；shadow_gate 检查收归 lifespan（main.py:196），本块不再
    # sys.exit——server 起不起与 engine 起不起解耦（影子期不足 server 仍起，手动 API 可用）。
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    logger.info("=== 自动交易引擎启动（AUTO_TRADE_MODE=%s，起 uvicorn 托管 engine）===", mode)
    run_server()
```

**4d. 更新模块 docstring**（line 1-54）顶部「定位」段，把「开发/调试常驻入口：`python -m trading`」的描述从「独立起 engine」改为「起 uvicorn」。最小改动：把 line 5-9 的「定位」段替换为：

old_string：
```python
============================================================================
定位（C-2 scheduling-orchestration Task 5/W1 重构后）
============================================================================
本入口现仅为**开发/调试常驻入口**，**不再是生产唯一入口**。生产路径由
``start_all.py`` 拉起的 **uvicorn server** 进程托管：server lifespan 内构造
TradingEngine 并起 APScheduler（engine 与 server 合并进同进程）。
```

new_string：
```python
============================================================================
定位（C-5 V1 进程模型统一后）
============================================================================
本入口起 **uvicorn server**（``presentation.server.main:app``），engine 由 server
lifespan 装配——与生产 ``start_all.py`` 链**同一入口**，消除历史双进程抢 session
（C-5 V1，[[qmt-connect-1-rootcause]] 教训）。端口 8000 天然单例（第二实例 bind
失败 exit），无需文件锁。``run_server()`` 是薄包装（live 不 reload，防 reloader
子进程抢 session）；``check_shadow_gate`` 影子期检查收归 lifespan（本块不 sys.exit）。
```

**4e. 清理无用 import**：`if __name__` 块不再用 `asyncio` / `sys`。但 `sys` 在模块顶层可能仍被其他地方用——检查 `trading/__main__.py` 顶部 `import sys`（line 60）是否还有引用；若 V1 后无引用则删除（`asyncio` import line 57 同理检查）。grep 确认：
Run: `grep -nE "\basyncio\b|\bsys\b" F:/quanter/trading/__main__.py`
若仅剩 import 行无使用，删除对应 import。

- [ ] **Step 5：跑测试验证通过**

Run: `pytest tests/trading/test_main.py -v`
Expected: 7 个用例全 PASS（`test_module_importable` / `test_run_forever_removed` / `test_run_server_is_callable` / `test_run_server_calls_uvicorn_port_8000` / `test_run_server_live_no_reload` / `test_run_server_dry_run_reload_true` / `test_no_asyncio_run_at_top_level`）。

- [ ] **Step 6：跑相邻测试确认无连带破坏**

Run: `pytest tests/trading/test_main_banner.py tests/trading/test_main_shadow_gate.py tests/trading/test_shadow_gate.py -v`
Expected: 全 PASS（`log_startup_banner` / `check_shadow_gate` 签名未动，仅搬走 `_run_forever`）。

- [ ] **Step 7：commit**

```bash
git add trading/__main__.py tests/trading/test_main.py
git commit -m "feat(c5-v1): __main__ 改 uvicorn 薄壳（消除双进程抢 session）

- 新增 run_server() 薄函数起 uvicorn :8000（live reload=False 防子进程抢 session）
- 废弃 _run_forever（engine 装配收归 lifespan，端口 8000 天然单例替代文件锁）
- if __name__ 不再 sys.exit shadow_gate（检查收归 lifespan main.py:196）
- 重写 test_main.py：删 3 个 _run_forever 契约锁，新增 run_server 契约锁（uvicorn
  调用参数 + live 不 reload + 顶层无裸 asyncio.run）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2 (V2)：lifespan 加 `log_startup_banner`（生产链 session 漂移可见）

**Files:**
- Modify: `presentation/server/main.py:191-198`（lifespan try 块内，`TradingEngine()` 前）
- Test: `tests/trading/test_main_banner.py`（新增 lifespan 调 banner 源码级断言）

**Interfaces:**
- Consumes: `trading.__main__.log_startup_banner`（V1 保留，纯函数读 `os.environ` + `logger.info`，无网关/scheduler 依赖）。
- Produces: lifespan 装配 engine 前打印 session/account/mode/口径 banner（生产链 session 漂移一眼可见，[[qmt-connect-1-rootcause]] 教训）。

- [ ] **Step 1：写失败测试（在 test_main_banner.py 追加）**

在 `tests/trading/test_main_banner.py` 末尾追加：

```python
def test_lifespan_calls_log_startup_banner():
    """C-5 V2：lifespan 装配 TradingEngine 前调 log_startup_banner（生产链 session 漂移可见）。

    物理意图（spec §3.2 · [[qmt-connect-1-rootcause]] 教训）：
        生产链 start_all→uvicorn→lifespan 之前无 banner，07-29 故障中 engine 进程内
        session=123456 而 .env=123458，无任何日志可对比。V2 把 banner 调用从 __main__
        搬到 lifespan，让生产链也在装配 engine 前固化 session/account/mode/口径到日志。
        本测试源码级断言 main.py lifespan 函数体含 log_startup_banner() 调用（与
        test_main.py 的源码级契约锁同范式，不起真实 server）。
    """
    import pathlib
    main_py = pathlib.Path("presentation/server/main.py")
    src = main_py.read_text(encoding="utf-8")
    # import 行必须把 log_startup_banner 从 trading.__main__ 引入
    assert "log_startup_banner" in src, (
        "main.py lifespan 必须引入并调用 log_startup_banner（C-5 V2，"
        "生产链 session 漂移可见性）")
    # 必须有实际调用（不是只 import 不调）
    assert "log_startup_banner()" in src, (
        "main.py lifespan 必须实际调用 log_startup_banner()（仅 import 不算）")
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/trading/test_main_banner.py::test_lifespan_calls_log_startup_banner -v`
Expected: FAIL（`main.py` 当前未调 `log_startup_banner()`）。

- [ ] **Step 3：实现——lifespan 加 banner 调用**

Edit `presentation/server/main.py`，定位 lifespan 内 TradingEngine 装配块（line 191-198）：

old_string：
```python
    try:
        from trading.engine import TradingEngine
        from trading.__main__ import check_shadow_gate
        eng = TradingEngine()
        await eng.bootstrap()
        if check_shadow_gate():
```

new_string：
```python
    try:
        from trading.engine import TradingEngine
        from trading.__main__ import check_shadow_gate, log_startup_banner
        # C-5 V2：装配 engine 前打启动 banner（session/account/mode/口径版本）。
        # 物理意图（spec §3.2 · [[qmt-connect-1-rootcause]]）：生产链 start_all→uvicorn
        # →lifespan 之前无 banner，session 漂移（进程内 123456 vs .env 123458）无日志可
        # 对比。banner 先于 bootstrap（含网关 connect）输出，便于排查 .env 漂移。
        log_startup_banner()
        eng = TradingEngine()
        await eng.bootstrap()
        if check_shadow_gate():
```

- [ ] **Step 4：跑测试验证通过**

Run: `pytest tests/trading/test_main_banner.py -v`
Expected: 2 用例全 PASS（既有 `test_startup_banner_logs_key_config` + 新增 `test_lifespan_calls_log_startup_banner`）。

- [ ] **Step 5：commit**

```bash
git add presentation/server/main.py tests/trading/test_main_banner.py
git commit -m "feat(c5-v2): lifespan 装配 engine 前调 log_startup_banner

生产链 start_all→uvicorn→lifespan 现也输出 session/account/mode/口径 banner，
session 漂移（进程内 vs .env）一眼可见（[[qmt-connect-1-rootcause]] 教训）。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3 (V3)：抽 `_gw_health_gate` + `_pre_open_gate` ② 段 DRY

**Files:**
- Modify: `trading/engine.py`（新增 `_gw_health_gate` method；`_pre_open_gate` ② 段 line 1907-1911 改调它）
- Test: `tests/trading/test_gw_health_gate.py`（新建，4 用例）
- 回归：`tests/trading/test_engine_pre_open_gate.py`（既有 9 用例无需改，即 DRY 后行为回归）

**Interfaces:**
- Consumes: `gw._connected`（bool 属性）、`gw.is_client_ready()`（sync 方法，`broker/qmt.py:311` 契约）。
- Produces: `TradingEngine._gw_health_gate(self, gw) -> tuple[bool, str]`（sync 方法；`(True, "")` 全绿，`(False, reason)` 锁态）。下游 Task 4 的 `_stoploss`/`_post_close` 依赖此签名。

- [ ] **Step 1：写失败测试（新建 test_gw_health_gate.py）**

创建 `tests/trading/test_gw_health_gate.py`：

```python
# -*- coding: utf-8 -*-
"""C-5 V3：_gw_health_gate 共享前置 gate 单测（从 _pre_open_gate ② 段抽）。

物理意图（spec §4.1）：触发点业务前显式探测网关健康，锁态时返 (False, reason)
让调用方 skip+CRITICAL 不跑业务（防静默全失败）。从 _pre_open_gate ② 段抽离，
共享给 _stoploss/_post_close，三入口同口径（与 _health_guard 不升 L1 自愈取向一致）。

测试边界：gw 用 MagicMock 模拟（_connected / is_client_ready 返指定值），不真连网关。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from trading.engine import TradingEngine


def test_gw_none_blocks():
    """gw=None → (False, '网关未连接')。"""
    eng = TradingEngine()
    ok, reason = eng._gw_health_gate(None)
    assert ok is False
    assert "网关" in reason


def test_gw_not_connected_blocks():
    """gw._connected=False → (False, '网关未连接')。"""
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = False
    ok, reason = eng._gw_health_gate(gw)
    assert ok is False
    assert "网关" in reason


def test_gw_connected_but_client_not_ready_blocks():
    """gw._connected=True 但 is_client_ready()=False → (False, '客户端未就绪')。"""
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = False
    ok, reason = eng._gw_health_gate(gw)
    assert ok is False
    assert "客户端" in reason


def test_gw_all_green_passes():
    """gw._connected=True 且 is_client_ready()=True → (True, '')。"""
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    ok, reason = eng._gw_health_gate(gw)
    assert ok is True
    assert reason == ""
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/trading/test_gw_health_gate.py -v`
Expected: 4 用例全 FAIL（`AttributeError: 'TradingEngine' object has no attribute '_gw_health_gate'`）。

- [ ] **Step 3：实现——在 TradingEngine 新增 `_gw_health_gate` method**

定位 `_pre_open_gate` 方法（engine.py:1876）上方，在 `# Task 8（C-2 S3）：pre_open 三段式前置 gate` 注释块之前插入新方法（与 `_pre_open_gate` 同为 TradingEngine method）：

```python
    def _gw_health_gate(self, gw) -> tuple[bool, str]:
        """C-5 V3：网关健康前置 gate（从 _pre_open_gate ② 段抽，共享给 _stoploss/_post_close）。

        物理意图（spec §4.1 · B 共享前置 gate）：
            触发点业务前显式探测网关健康，锁态时返 ``(False, reason)`` 让调用方
            skip + CRITICAL 不跑业务（防静默全失败），与 ``_pre_open_gate`` ② 段
            + ``_health_guard`` 自愈取向一致（不停调度，等 60s 自愈恢复 live）。

        判据（与 _pre_open_gate ② 段逐行等价，DRY 抽离零行为变更）：
            ① ``gw is None`` 或 ``gw._connected=False`` → ``"网关未连接"``；
            ② ``gw.is_client_ready()=False`` → ``"miniQMT 客户端未就绪"``。
        ``is_client_ready`` 是纯文件 mtime 探测（broker/qmt.py:311），不触达 xtquant，
        CI/单测/无 SDK 环境安全调用。

        Args:
            gw: 交易网关实例（``get_gateway()`` 取，可能为 None）。鸭子类型：读
                ``gw._connected`` 与调 ``gw.is_client_ready()``。

        Returns:
            ``(True, "")`` 网关健康；``(False, reason)`` 锁态，reason 简短中文。
        """
        if gw is None or not getattr(gw, "_connected", False):
            return False, "网关未连接"
        if not gw.is_client_ready():
            return False, "miniQMT 客户端未就绪"
        return True, ""
```

- [ ] **Step 4：跑新测试验证通过**

Run: `pytest tests/trading/test_gw_health_gate.py -v`
Expected: 4 用例全 PASS。

- [ ] **Step 5：DRY——`_pre_open_gate` ② 段改调 `_gw_health_gate`**

Edit `trading/engine.py` `_pre_open_gate` ② 段（line 1907-1911）：

old_string：
```python
        # ② 网关健康（探测，无写副作用）
        if gw is None or not getattr(gw, "_connected", False):
            return False, "网关未连接"
        if not gw.is_client_ready():
            return False, "miniQMT 客户端未就绪"
```

new_string：
```python
        # ② 网关健康（探测，无写副作用）—— C-5 V3 DRY：改调共享 _gw_health_gate
        # （与 _stoploss/_post_close 三入口同口径；行为与原内联逐行等价）。
        gw_ok, gw_reason = self._gw_health_gate(gw)
        if not gw_ok:
            return False, gw_reason
```

- [ ] **Step 6：回归 _pre_open_gate 既有测试（DRY 后行为不变）**

Run: `pytest tests/trading/test_engine_pre_open_gate.py -v`
Expected: 9 用例全 PASS（`test_gateway_none_blocks` / `test_gateway_not_connected_blocks` / `test_gateway_client_not_ready_blocks` / `test_all_green_*` 等行为零变更，reason 文案「网关未连接」/「miniQMT 客户端未就绪」逐字保持）。

- [ ] **Step 7：commit**

```bash
git add trading/engine.py tests/trading/test_gw_health_gate.py
git commit -m "feat(c5-v3): 抽 _gw_health_gate + _pre_open_gate ② 段 DRY

- 新增 TradingEngine._gw_health_gate(gw) -> (bool, str)（从 _pre_open_gate ② 抽）
- _pre_open_gate ② 网关健康段改调共享方法（三入口同口径，零行为变更）
- 新建 test_gw_health_gate.py（4 用例：None/未连接/未就绪/全绿）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4 (V4)：`_stoploss`/`_post_close` 入口调 `_gw_health_gate`（锁态 skip+CRITICAL）

**Files:**
- Modify: `trading/engine.py:2407-2447`（`_stoploss` 入口加 gate）+ `engine.py:2543-2548`（`_post_close` 入口加 gate）
- Test: `tests/trading/test_stoploss_post_close_gate.py`（新建，4 用例）
- Modify: `tests/trading/test_engine_stoploss_inject.py`（3 既有用例补 `patch get_gateway`）

**Interfaces:**
- Consumes: `TradingEngine._gw_health_gate`（Task 3 产出）；`engine.get_gateway()`（顶层函数 engine.py:441，返 gw 或 None）；`engine._alert_critical(msg)`（顶层函数 engine.py:94，fire_and_forget 钉钉 CRITICAL）。
- Produces: `_stoploss`/`_post_close` 入口语义变更——锁态 skip+CRITICAL 不跑业务（不停调度，与 `_pre_open_gate` + `_health_guard` 一致）。

- [ ] **Step 1：写失败测试（新建 test_stoploss_post_close_gate.py）**

创建 `tests/trading/test_stoploss_post_close_gate.py`：

```python
# -*- coding: utf-8 -*-
"""C-5 V4：_stoploss / _post_close 入口 _gw_health_gate 前置（锁态 skip+CRITICAL）。

物理意图（spec §4.3 · B 共享前置 gate）：
    两 job 在 @_critical_guard 后、交易日守卫前调 _gw_health_gate，网关锁态时
    _alert_critical + return 不跑业务（不调 stop_loss_monitor / post_close），
    不停调度（等 _health_guard 60s 自愈恢复 live）。与 _pre_open_gate 网关锁态
    skip+CRITICAL 同口径；与 _health_guard 不升 L1 自愈取向一致（C-4 决议）。

测试边界：
    gw 用 MagicMock 模拟锁态（_connected=False），patch stop_loss_monitor / post_close
    为哨兵断言「未被触达」。交易日守卫 patch is_trading_day=True（隔离 gate 与交易日）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from trading.engine import TradingEngine


def test_stoploss_gw_locked_skips_and_alerts():
    """gw 锁态（_connected=False）→ _stoploss skip+CRITICAL，不调 stop_loss_monitor。

    断言：① _alert_critical 被调（CRITICAL 推钉钉）；② stop_loss_monitor 未被调
    （不查 plan、不发卖）；③ 不停调度（无 _CriticalHalt 抛出，方法正常 return）。
    """
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = False   # 锁态：未连接
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine._alert_critical") as ac, \
         patch("trading.engine.calendar.is_trading_day", return_value=True), \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon, \
         patch("trading.engine.trading_plan.load_plan") as lp:
        asyncio.run(eng._stoploss())   # 不抛 _CriticalHalt（gate skip 不停调度）
    ac.assert_called_once()       # CRITICAL 告警推送
    mon.assert_not_called()       # 业务未触达
    lp.assert_not_called()        # 连 plan 都不查（gate 在交易日守卫前更前）


def test_stoploss_gw_green_proceeds_to_monitor():
    """gw 绿（_connected=True + ready）→ _stoploss 放行调 stop_loss_monitor（回归）。

    隔离 gate 通过后下游仍走原逻辑（load_plan → stop_prices 注入 → monitor）。
    断言 stop_loss_monitor 被调（无计划则 stop_prices=None，但 monitor 一定被触达）。
    """
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine._alert_critical") as ac, \
         patch("trading.engine.calendar.is_trading_day", return_value=True), \
         patch("trading.engine.trading_plan.load_plan", return_value=None), \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        asyncio.run(eng._stoploss())
    ac.assert_not_called()        # gate 绿不告警
    mon.assert_called_once()      # 业务放行


def test_post_close_gw_locked_skips_and_alerts():
    """gw 锁态 → _post_close skip+CRITICAL，不调 post_close（不对账）。"""
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = False
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine._alert_critical") as ac, \
         patch("trading.engine.calendar.is_trading_day", return_value=True), \
         patch("trading.engine.post_close", new=AsyncMock()) as pc, \
         patch("trading.position_book.get_local_positions", return_value={}):
        asyncio.run(eng._post_close())
    ac.assert_called_once()
    pc.assert_not_called()        # 对账业务未触达


def test_post_close_gw_green_proceeds():
    """gw 绿 → _post_close 放行调 post_close（回归）。"""
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine._alert_critical") as ac, \
         patch("trading.engine.calendar.is_trading_day", return_value=True), \
         patch("trading.engine.post_close", new=AsyncMock()) as pc, \
         patch("trading.position_book.get_local_positions", return_value={}):
        asyncio.run(eng._post_close())
    ac.assert_not_called()
    pc.assert_called_once()
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/trading/test_stoploss_post_close_gate.py -v`
Expected: 4 用例中 `test_stoploss_gw_locked_skips_and_alerts` / `test_post_close_gw_locked_skips_and_alerts` FAIL（当前无 gate，锁态仍跑业务，`_alert_critical` 未调、`stop_loss_monitor`/`post_close` 被调）；`test_stoploss_gw_green_proceeds_to_monitor` / `test_post_close_gw_green_proceeds` 可能 PASS（gw 绿本就放行）。

- [ ] **Step 3：实现——`_stoploss` 入口加 gate**

Edit `trading/engine.py` `_stoploss` 方法（engine.py:2442-2447），在 docstring 结束后、`today = datetime.now()...` 前插入 gate。

定位锚点（`_stoploss` docstring 末尾两行 + today 行，唯一）：

old_string：
```python
            需在盘中按持仓最高价动态更新 stop_prices map，属另一个 follow-up，不在本 task 内。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        # 交易日守卫（Task 8 fix · review I1）：IntervalTrigger 无 1-5 工作日过滤，
```

new_string：
```python
            需在盘中按持仓最高价动态更新 stop_prices map，属另一个 follow-up，不在本 task 内。
        """
        # C-5 V4 B：网关健康前置 gate（@_critical_guard 后、交易日守卫前）。
        # 物理意图（spec §4.3）：gw 锁态时 skip+CRITICAL 不跑业务（不查 plan、不调
        # stop_loss_monitor），与 _pre_open_gate 网关锁态 skip 同口径；与 _health_guard
        # 不升 L1 自愈取向一致（C-4 决议）—— 等待 60s 自愈恢复 live，而非 _halt 停调度。
        gw = get_gateway()
        ok, reason = self._gw_health_gate(gw)
        if not ok:
            _alert_critical(f"stop_loss 跳过：{reason}（gw 锁态，等 _health_guard 自愈）")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        # 交易日守卫（Task 8 fix · review I1）：IntervalTrigger 无 1-5 工作日过滤，
```

- [ ] **Step 4：实现——`_post_close` 入口加 gate**

Edit `trading/engine.py` `_post_close` 方法（engine.py:2543-2548）：

old_string：
```python
    @_critical_guard
    async def _post_close(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if not calendar.is_trading_day(today):
            logger.info("post_close 跳过：今日非交易日 %s", today)
            return
```

new_string：
```python
    @_critical_guard
    async def _post_close(self) -> None:
        # C-5 V4 B：网关健康前置 gate（与 _stoploss 同口径，spec §4.3）。
        # gw 锁态时 skip+CRITICAL 不跑对账业务（防基于陈旧/缺失快照误判 drift），
        # 等 _health_guard 自愈；不停调度（与 _pre_open_gate + _health_guard 一致）。
        gw = get_gateway()
        ok, reason = self._gw_health_gate(gw)
        if not ok:
            _alert_critical(f"post_close 跳过：{reason}（gw 锁态，等 _health_guard 自愈）")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if not calendar.is_trading_day(today):
            logger.info("post_close 跳过：今日非交易日 %s", today)
            return
```

- [ ] **Step 5：跑新测试验证通过**

Run: `pytest tests/trading/test_stoploss_post_close_gate.py -v`
Expected: 4 用例全 PASS。

- [ ] **Step 6：改既有 stoploss_inject 测试（补 patch get_gateway 让 gate 放行）**

`_stoploss` 入口现先调 `get_gateway()`，既有 `test_engine_stoploss_inject.py` 3 用例需补 patch 返 connected+ready gw，否则 gate skip 到不了 stop_prices 注入逻辑。

Edit `tests/trading/test_engine_stoploss_inject.py`：

**6a. `test_stoploss_injects_stop_prices_from_plan`**——在 `with patch(...)` 链最前补 gw patch：

old_string：
```python
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine.calendar") as cal, \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
```

new_string：
```python
    # C-5 V4：_stoploss 入口先过 _gw_health_gate，须 patch get_gateway 返 connected+ready
    # gw 让 gate 放行，否则 gate skip 到不了 stop_prices 注入逻辑。
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine.calendar") as cal, \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
```

**6b. `test_stoploss_no_plan_injects_none`**——同样补 gw patch：

old_string：
```python
    with patch("trading.engine.trading_plan.load_plan", return_value=None), \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        asyncio.run(eng._stoploss())
    _, kwargs = mon.call_args
    assert kwargs.get("stop_prices") in (None, {})
```

new_string：
```python
    # C-5 V4：补 connected+ready gw 让 gate 放行（见上用例同款）。
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine.trading_plan.load_plan", return_value=None), \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        asyncio.run(eng._stoploss())
    _, kwargs = mon.call_args
    assert kwargs.get("stop_prices") in (None, {})
```

**6c. `test_stoploss_skips_non_trading_day`**——此用例断言「非交易日不查 plan 不调 monitor」。V4 后 gate 在交易日守卫前：若 gw 锁态，gate 先 skip（原因变成 gate 而非非交易日）；要让「非交易日 skip」语义仍可测，须 patch gw 绿让 gate 放行，再断言非交易日守卫拦截。

old_string：
```python
    eng = TradingEngine()
    with patch("trading.engine.calendar.is_trading_day", return_value=False), \
         patch("trading.engine.trading_plan.load_plan") as lp, \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        asyncio.run(eng._stoploss())
    lp.assert_not_called()    # 非交易日不查 plan
    mon.assert_not_called()   # 非交易日不调 monitor
```

new_string：
```python
    eng = TradingEngine()
    # C-5 V4：gate 在交易日守卫前，须 gw 绿让 gate 放行，才能测到「非交易日守卫」拦截。
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine.calendar.is_trading_day", return_value=False), \
         patch("trading.engine.trading_plan.load_plan") as lp, \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        asyncio.run(eng._stoploss())
    lp.assert_not_called()    # 非交易日不查 plan
    mon.assert_not_called()   # 非交易日不调 monitor
```

**6d. 文件顶部 import 补 `MagicMock`**：确认 `from unittest.mock import AsyncMock, patch` 含 `MagicMock`，若无需补。

old_string：
```python
from unittest.mock import AsyncMock, patch
```

new_string：
```python
from unittest.mock import AsyncMock, MagicMock, patch
```

- [ ] **Step 7：跑既有 stoploss 测试验证通过**

Run: `pytest tests/trading/test_engine_stoploss_inject.py -v`
Expected: 3 用例全 PASS（gate 放行后下游注入/非交易日 skip 行为不变）。

- [ ] **Step 8：跑 _stoploss/_post_close 全套相关测试确认无连带破坏**

Run: `pytest tests/trading/test_engine_stoploss_inject.py tests/trading/test_stoploss_post_close_gate.py tests/trading/test_critical_guard.py tests/trading/test_stop_loss_l1_halt.py tests/trading/test_stop_loss_monitor_decide_exit.py tests/trading/test_l2_aggregated_critical.py -v`
Expected: 全 PASS（_stoploss L1 / 聚合 CRITICAL / decide_exit 等既有行为不受 gate 前置影响——gate 仅在 gw 锁态时 skip，gw 绿时下游全不变；若某既有测试假定 `_stoploss` 不调 get_gateway，按 6a/6b/6c 同款补 gw patch）。

**注**：若 Step 8 暴露其他测试因「`_stoploss`/`_post_close` 入口新增 `get_gateway()` 调用」而失败（如 test_engine.py / test_e2e_trading_flow.py），按同款补 `patch("trading.engine.get_gateway", return_value=gw)` 让 gate 放行。grep 定位受影响测试：
Run: `grep -rln "_stoploss\|_post_close" F:/quanter/tests --include="*.py"`
对每个命中文件，确认其 patch 链是否含 `get_gateway`，未含的补绿 gw。

- [ ] **Step 9：commit**

```bash
git add trading/engine.py tests/trading/test_stoploss_post_close_gate.py tests/trading/test_engine_stoploss_inject.py
git commit -m "feat(c5-v4): _stoploss/_post_close 入口加 _gw_health_gate（锁态 skip+CRITICAL）

- 两 job 在 @_critical_guard 后、交易日守卫前调 _gw_health_gate
- gw 锁态 → _alert_critical + return 不跑业务（不调 stop_loss_monitor/post_close）
- 不停调度（与 _pre_open_gate + _health_guard 自愈取向一致，C-4 决议）
- 新建 test_stoploss_post_close_gate.py（4 用例：两 job 锁态 skip + 绿放行）
- 既有 test_engine_stoploss_inject.py 补 patch get_gateway 让 gate 放行

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5 (V5)：schtasks/start_all 回归 smoke + 全量回归 + spec §9 验收

**Files:** 无代码改动（验证性 Task；若验收发现 doc 需更新，单独 commit）。

**Interfaces:** 消费 Task 1-4 全部产出。

- [ ] **Step 1：源码级断言 schtasks 命令字面不变（spec §3.4）**

Run: `grep -nE "python -m trading" F:/quanter/scripts/*.bat F:/quanter/ops/*.py 2>/dev/null`
Expected: 命中 `scripts/run_trading_engine.bat`（开发守护入口，若存在）。命令字面 `python -m trading` 不变（V1 内部起 uvicorn，命令外观不变）。

确认 `scripts/start_all.bat` 调 `.venv310\Scripts\python.exe ops/start_all.py`（生产链，不走 `python -m trading`，V1 不影响）。

确认 `ops/start_all.py:89-90` 注释「不再单独起 python -m trading（已合并进 lifespan）」——生产链已用 uvicorn，V1 改造的是开发/调试入口，无生产链回归风险。

- [ ] **Step 2：端口 8000 单例 smoke（手动，spec §6/§9 验收 3）**

手动验证（集成测试，CI 不自动化）：
1. 终端 A：`cd F:/quanter && .venv310/Scripts/python.exe -m trading` → uvicorn 起在 :8000，日志含「=== 自动交易引擎启动（...，起 uvicorn 托管 engine）===」+ 启动 banner（session/account/mode/口径）。
2. 终端 B：`.venv310/Scripts/python.exe -m trading` → 第二实例 bind 8000 失败（`WSAEADDRINUSE` / `Address already in use`）→ uvicorn exit → 不到 lifespan 装 engine → **天然不双进程**。
3. 终端 A Ctrl-C 优雅退出。

记录 smoke 结果到 commit message（若手动跑通）或 plan review note。

- [ ] **Step 3：全量回归（spec §7 验收 7 · C-4 后 1146 基线零退化）**

Run: `cd F:/quanter && .venv310/Scripts/python.exe -m pytest tests/ -q`
Expected: `1146 passed`（C-4 基线）+ 本期新增 11 用例（V1: 7 + V2: 1 + V3: 4 + V4: 4 = 16 新增，减去 V1 删除的 3 个 _run_forever 用例 = 净 +13）→ 约 **1159 passed / 0 failed**。允许数差异（若 V1/V4 改造时连带调整了既有测试数），但 **0 failed** 是硬指标。

若有 failed：按失败信息回 Task 1-4 修复，**不许跳过**。

- [ ] **Step 4：spec §9 验收标准逐条核对**

逐条核对 spec §7（验收标准 1-7）：

| # | 验收项 | 核对方式 |
|---|---|---|
| 1 | `__main__` `if __name__` 起 uvicorn（不再独立 engine）；`_run_forever` 废弃 | Task 1 Step 5 `test_run_forever_removed` + `test_run_server_calls_uvicorn_port_8000` PASS |
| 2 | lifespan 装配 engine 前调 `log_startup_banner` | Task 2 Step 4 `test_lifespan_calls_log_startup_banner` PASS |
| 3 | 端口 8000 单例（第二实例 exit；不开 `SO_REUSEPORT`） | Step 2 手动 smoke + `run_server()` 未传 `reuse_port`（Task 1 代码） |
| 4 | schtasks 命令 `python -m trading` 行为变起 server，`start_all.bat` + `manage_ops_schtasks` 回归通过 | Step 1 源码断言 + `start_all.py:89-90` 生产链已 uvicorn |
| 5 | `_gw_health_gate` 抽出；`_pre_open_gate` ② DRY（行为不变） | Task 3 Step 4+6（新单测 + 既有 9 用例回归 PASS） |
| 6 | `_stoploss`/`_post_close` 入口调 gate，锁态 skip+CRITICAL 不跑业务（不停调度） | Task 4 Step 5（4 用例 PASS） |
| 7 | 全量回归零退化（C-4 后 1146 基线） | Step 3 `0 failed` |

- [ ] **Step 5：final commit（如有 doc/review note 更新）**

若 Step 1-4 全绿无代码改动，本 Task 无 commit（Task 1-4 已分别 commit）。若发现 doc 需补（如 __main__ docstring 微调、spec §9 验收记录），单独 commit：

```bash
git add <改动文件>
git commit -m "test(c5-v5): 全量回归 + e2e gate（spec §9 验收 7 条全绿）

- 全量回归 1159 passed / 0 failed（C-4 基线 1146 + 净增 13）
- spec §7 验收 1-7 逐条全绿
- 端口 8000 单例 smoke 通过（手动）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

- [ ] **Step 6：更新 memory（C5 完成状态）**

C5 全部 Task 完成且全量回归绿后，更新 `C:\Users\yzzhan\.claude\projects\F--quanter\memory\c5-process-model-gate-status.md`（新建）+ `MEMORY.md` 索引行。内容模板：
- merged master（commit hash，日期 2026-08-XX）
- C 统一 server：`__main__` run_server uvicorn 薄壳 + 端口 8000 单例；`_run_forever` 废弃
- B 共享 gate：`_gw_health_gate` 抽出，`_stoploss`/`_post_close`/`_pre_open_gate` 三入口同口径
- lifespan 加 `log_startup_banner`
- live P0 待办不变（部分成交精度/熔断/trailing/EMT 行情）

---

## Self-Review

**1. Spec 覆盖**：
- spec §3.1（`__main__` uvicorn 薄壳 + 废弃 `_run_forever` + live 不 reload）→ Task 1 ✓
- spec §3.2（lifespan 加 `log_startup_banner`）→ Task 2 ✓
- spec §3.3（端口 8000 单例 + 不开 SO_REUSEPORT）→ Task 1（`run_server` 不传 reuse_port）+ Task 5 Step 2 手动 smoke ✓
- spec §3.4（schtasks 命令字面不变 + start_all 回归）→ Task 5 Step 1 ✓
- spec §4.1（抽 `_gw_health_gate`）→ Task 3 ✓
- spec §4.2（`_pre_open_gate` ② DRY）→ Task 3 Step 5 ✓
- spec §4.3（`_stoploss`/`_post_close` 入口调 gate）→ Task 4 ✓
- spec §7 验收 1-7 → Task 5 Step 4 逐条 ✓
- spec §6 测试策略（C: main/banner/端口/schtasks；B: gate 单测/两 job gate/pre_open 回归）→ Task 1-5 全覆盖 ✓

**2. Placeholder 扫描**：无 TBD/TODO；每个 code step 含完整可执行代码（run_server / _gw_health_gate / _stoploss gate / _post_close gate / 全部测试用例）；每个 Edit 含精确 old_string/new_string。Task 4 Step 8 的「若连带破坏按同款补 patch」给出 grep 定位 + 修复模式（非 placeholder，是防御性指引）。

**3. 类型一致性**：
- `_gw_health_gate(self, gw) -> tuple[bool, str]`：Task 3 定义、Task 4 消费，签名一致 ✓
- `run_server() -> None`：Task 1 定义、test_main.py 消费，一致 ✓
- `log_startup_banner()`：Task 1 保留（既有）、Task 2 lifespan 消费，签名不变 ✓
- reason 文案「网关未连接」/「miniQMT 客户端未就绪」：Task 3 `_gw_health_gate` 与原 `_pre_open_gate` ② 段逐字一致，`test_engine_pre_open_gate.py` 回归断言「网关」/「客户端」不破坏 ✓

**4. 连带影响已显式处理**：
- `test_main.py` 4 个 `_run_forever` 契约锁 → Task 1 Step 2 整文件重写 ✓
- `test_engine_stoploss_inject.py` 3 用例未 patch get_gateway → Task 4 Step 6 显式补 ✓
- 其他 `_stoploss`/`_post_close` 测试可能连带 → Task 4 Step 8 grep + 同款 patch 指引 ✓
