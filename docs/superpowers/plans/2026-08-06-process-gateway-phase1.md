# 进程·网关·自动化链路 Phase 1 实施计划（process-gateway-phase1）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通自动化链路的第一段——修掉 09:22 集合竞价误拦、台账 done 掩盖 0 成交、版本/重启不可见、schtasks 触发器错配、生产 fail-closed 缺失，并落地进程单一所有者（supervisor + 原子重启 + 测试隔离 + dev 解耦），让 08-06 09:22 不再复现 08-05 废单。

**Architecture:** 两条并行线：(1) Phase A 前置最小修复——只动「链路调通验收」要求的点（session 关、台账语义、版本告警、schtasks、fail-closed、audit 拓扑），不批量删风控（红线 1）；(2) Phase B1 进程治理——`ops/trading_supervisor.py` 做三合一校验（端口/pid 文件/session 锁），`ops/restart_trading.py` 做原子重启唯一入口，`QUANTER_TESTING`/`server_lifecycle` 隔离测试，dev.py 统一走 `python -m trading` 并跳过 connect bot。

**Tech Stack:** Python 3.10（`.venv310`）、FastAPI/uvicorn、APScheduler、sqlite3、pytest、Windows `netstat`/`taskkill`/`schtasks`/PowerShell ScheduledTasks、钉钉 notifier。

## Global Constraints

- **语言**：所有对话/注释/文档 100% 中文；注释写「为什么」不只写「是什么」。
- **定位用符号名，禁用行号**：本 plan 引用「函数名 + 语义段」，执行者用 `rg` / codegraph 找当前行号（master 已到 033e4a85，行号持续漂移）。
- **TDD**：每个 task 先写失败测试再改实现；测试命令 `.venv310/Scripts/python.exe -m pytest <path> -v`。
- **反魔法**：不引 psutil/新重依赖；进程信息用标准库 + `netstat`/`Get-Process`/`tasklist`。
- **Windows 限制诚实标注**：跨进程命令行取不到时降级为「exe 路径 + 端口/pid 文件」判定，绝不假装精确。
- **不自动 taskkill**：supervisor 只告警/拒绝；`restart_trading.py` 默认 dry-run，`--yes` 才动手。
- **QUANTER_TESTING/QUANTER_DEV_MODE 缺省不生效**：生产启动链不设置即行为不变。
- **回滚**：每个 task 独立 commit；A/B 两组互不阻塞，可分别 revert。

## File Structure

| 文件 | 职责 | 本 plan 动作 |
|---|---|---|
| `presentation/server/services/trading_service.py` | `_in_a_share_session` 时段判定 | A1 改 09:15 起点 |
| `trading/engine.py` | pre_open 台账包裹 + 逐单统计 | A2 加 partial/failed 语义 |
| `trading/catchup.py` | C-8 补跑 | A2 钉住 failed 重试 |
| `trading/__main__.py` | 启动入口 + banner | A3/A5/B3 加 git 版本、require-live、QUANTER_TESTING 跳过 |
| `scripts/start_server.bat` | 生产启动链 | A5 加 QUANTER_REQUIRE_LIVE=1 |
| `ops/dev.py` | 开发启动器 | A5 改走 `python -m trading` + dev env |
| `presentation/server/main.py` | lifespan 装配 | A5 测试/dev 跳过 connect bot |
| `ops/manage_ops_schtasks.py` | schtasks 注册 | A4 ONSTART + RestartOnFailure |
| `scripts/audit_ssot.py` | 巡检 | A6 进程拓扑三项 + 去 wmic |
| `ops/trading_supervisor.py` | 进程超级管理器 | B1 新建 |
| `ops/restart_trading.py` | 原子重启 | B2 新建 |
| `pytest.ini` | 测试排除 | B3 加 server_lifecycle |
| `docs/superpowers/runbooks/2026-08-04-gateway-ops.md` | 运维 SOP | B5 更新启动/重启入口 |

---

# Phase A 前置：链路调通最小修复

## Task A1: session 关上午起点 09:30 → 09:15（含集合竞价）

**Files:**
- Modify: `presentation/server/services/trading_service.py` `_in_a_share_session`
- Test: `tests/trading/test_trading_service_session.py`（新建）

**Interfaces:**
- Produces: `_in_a_share_session(now: datetime | None = None) -> bool`——`now` 可注入便于单测；默认 `datetime.now()`；工作日上午 09:15–11:30、下午 13:00–15:00 为 True。

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""A1 回归：pre_open 09:22 集合竞价窗口必须被放行（08-05 废单根因）。"""
from datetime import datetime

from presentation.server.services.trading_service import _in_a_share_session


def test_session_allows_call_auction_0915():
    """09:22（集合竞价 09:15-09:25 内）必须放行——pre_open 调度在此刻挂单。"""
    assert _in_a_share_session(datetime(2026, 8, 5, 9, 22)) is True


def test_session_blocks_before_0915():
    """09:10 仍拦截（隔夜保护不能破）。"""
    assert _in_a_share_session(datetime(2026, 8, 5, 9, 10)) is False


def test_session_blocks_weekend():
    """周末必须拦截（防周末误单）。"""
    assert _in_a_share_session(datetime(2026, 8, 8, 10, 0)) is False  # 周六
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_trading_service_session.py -v`
Expected: FAIL（`test_session_allows_call_auction_0915` 断言 False→True）

- [ ] **Step 3: 实现**

```python
def _in_a_share_session(now=None) -> bool:
    """粗略判断当前是否 A 股交易时段（09:15-11:30 / 13:00-15:00，工作日）。

    A1（08-05 废单根治）：上午起点 09:30 → 09:15，含集合竞价——pre_open 调度在
    09:22 挂单，旧口径把自家调度时间当非法时段（300358 废单根因）。隔夜/周末保护
    仍保留（09:15 前、周末均拦）。
    Why now 可注入：纯函数便于单测（避免 datetime.now() 不可控）。
    """
    from datetime import datetime
    now = now or datetime.now()
    if now.weekday() >= 5:  # 5=周六 6=周日
        return False
    t = now.hour * 60 + now.minute
    morning = 9 * 60 + 15 <= t <= 11 * 60 + 30
    afternoon = 13 * 60 <= t <= 15 * 60
    return morning or afternoon
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_trading_service_session.py tests/test_risk_shield.py -v`
Expected: PASS（既有 `test_block_session` 走 `in_session=False` 注入，不受影响）

- [ ] **Step 5: Commit**

```bash
git add presentation/server/services/trading_service.py tests/trading/test_trading_service_session.py
git commit -m "fix(risk): A1 session 关上午起点 09:30→09:15 含集合竞价（08-05 09:22 废单根治）"
```

---

## Task A2: pre_open 台账 partial/failed 语义 + C-8 重试钉住

**Files:**
- Modify: `trading/engine.py` `pre_open` 包裹 + `_pre_open_impl` 返回
- Test: `tests/trading/test_engine.py`（加台账状态用例）、`tests/trading/test_catchup.py`（加 failed 重试用例）

**Interfaces:**
- Produces: `_pre_open_impl` 返回 `{"submitted", "rejected", "total", "mode"}`；`pre_open` 落台账：gate 未过 → `skipped`；`submitted>0` → `done`；`mode=live 且 total>0 且 submitted=0` → `failed`（message 含 submitted/rejected/total）。
- Consumes: `_pre_open_impl` 内既有 `n_rejected` / `len(signals)` 计数（Grep `n_rejected` 定位）。

- [ ] **Step 1: 写失败测试**

```python
def test_pre_open_live_all_rejected_marks_failed(monkeypatch, tmp_path, caplog):
    """A2: live 有计划但 0 成交 → 台账 failed（不再 done 掩盖），message 含 submitted=0。"""
    from trading import engine, job_ledger
    import trading.state_store as ss
    db = str(tmp_path / "s.db")
    monkeypatch.setattr(ss, "_DEFAULT_DB", db)
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    job_ledger.init_db()
    # 构造有计划单但 submit 全拒：gate 全过 + _submit 抛 RuntimeError
    calls = {"submitted": 0}
    async def fake_submit(*a, **kw):
        raise RuntimeError("被风控挡板拒")
    monkeypatch.setattr(engine, "_submit", fake_submit)
    engine.load_plan = lambda date: {"confirmed": True, "orders": [
        {"order": {"symbol": "300358.SZ", "qty": 5700.0, "side": "buy", "price": 8.75}}]}
    # gate 全过（mock 实例）
    ...
    result = await engine.pre_open("2026-08-05")
    status = job_ledger.latest_status("pre_open", "2026-08-05")
    assert result["submitted"] == 0
    assert status == "failed"
```

> 执行者注意：上例的 gate/plan 构造按现有 `tests/trading/test_engine.py` 的 pre_open 测试范式补全
> （Grep `test_pre_open` 看既有 mock 范式）；核心断言是 `submitted=0 且有单 → failed`。

```python
def test_catchup_retries_failed_pre_open(monkeypatch, tmp_path):
    """A2: 台账 failed 在 C-8 窗口内必须重试（done 才跳过）。"""
    from trading import catchup, job_ledger
    job_ledger.init_db()
    job_ledger.finish_run("pre_open", "2026-08-05", "failed", "submitted=0")
    ran = []
    async def fake_pre_open(date):
        ran.append(date)
    monkeypatch.setattr("trading.catchup.pre_open", fake_pre_open)
    monkeypatch.setattr("trading.catchup.clock", _FakeClock())  # 窗口内 now
    ok, note = await catchup._catchup_pre_open()
    assert ok is True and ran == ["2026-08-05"]
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py -k all_rejected tests/trading/test_catchup.py -k retries_failed -v`
Expected: FAIL（当前 status 恒 done；`failed` 虽会重试但无测试钉住）

- [ ] **Step 3: 实现**

`_pre_open_impl` 末尾返回改为：

```python
    return {"submitted": n_submitted, "rejected": n_rejected,
            "total": len(signals), "mode": _mode()}
```

`pre_open` 包裹状态判定改为：

```python
    if result.get("skipped") or result.get("reason"):
        status = "skipped"
        message = str(result.get("skipped") or result.get("reason") or "")
    elif result.get("submitted", 0) > 0:
        status = "done"
        message = ""
    elif result.get("mode") == "live" and result.get("total", 0) > 0:
        # A2：live 有计划单但 0 成交 = 失败态（08-05 用 done 掩盖 0/1 被拒的教训）。
        # failed 不在 catchup 的 running/done 跳过集 → C-8 窗口内自动重试（has_order 幂等兜底）。
        status = "failed"
        message = (f"submitted=0/{result.get('total')} rejected={result.get('rejected')}")
    else:
        status = "done"
        message = ""
```

`catchup._catchup_pre_open` 无需改逻辑（`failed` 本就不在跳过集），补测试钉住即可。

- [ ] **Step 4: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py tests/trading/test_catchup.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/engine.py tests/trading/test_engine.py tests/trading/test_catchup.py
git commit -m "fix(engine): A2 pre_open 台账 failed 语义（submitted=0 有计划单不再 done 掩盖）+ C-8 重试钉住"
```

---

## Task A3: P0-3 启动 banner 打印 git 版本 + 启动时间

**Files:**
- Modify: `trading/__main__.py` `log_startup_banner` + 新 helper `_git_rev`
- Test: `tests/trading/test_main.py`（加 caplog 断言）

**Interfaces:**
- Produces: `_git_rev() -> str`（`git -C <ROOT> rev-parse --short HEAD`，失败返 `"unknown"`）；banner 追加 `git=<rev> started=<ISO>`。

- [ ] **Step 1: 写失败测试**

```python
def test_log_startup_banner_includes_git_rev_and_started(monkeypatch, caplog):
    """A3: banner 必须含 git 版本与启动时间（代码更新后未重启可人工/自动识别）。"""
    import logging
    import trading.__main__ as m
    monkeypatch.setattr(m, "_git_rev", lambda: "abc1234")
    with caplog.at_level(logging.INFO, logger="trading.__main__"):
        m.log_startup_banner()
    text = caplog.text
    assert "git=abc1234" in text
    assert "started=" in text
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_main.py::test_log_startup_banner_includes_git_rev_and_started -v`
Expected: FAIL（banner 无 git/started）

- [ ] **Step 3: 实现**

```python
def _git_rev() -> str:
    """当前 HEAD 短哈希（P0-3；失败降级 unknown，不阻断启动）。"""
    try:
        import subprocess
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[1]),
             "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"
```

`log_startup_banner` 日志串末尾追加：

```python
    logger.info(
        "=== 启动 banner === session=%s account=%s userdata=%s mode=%s confirm=%s | "
        "git=%s started=%s | 口径: eod=next_trading_day, pre_open=today（标的 T+1 对齐）",
        os.environ.get("QMT_SESSION_ID", "?"),
        os.environ.get("QMT_ACCOUNT_ID", "?"),
        os.environ.get("QMT_USERDATA_PATH", "?"),
        os.environ.get("AUTO_TRADE_MODE", "?"),
        os.environ.get("AUTO_CONFIRM_PLAN", "?"),
        _git_rev(),
        datetime.now().isoformat(timespec="seconds"),
    )
```

（`datetime` 已在模块顶层 import；`Path` 需在 `_git_rev` 内 import 或复用模块顶部。）

- [ ] **Step 4: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_main.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/__main__.py tests/trading/test_main.py
git commit -m "feat(main): A3 启动 banner 打印 git 版本 + 启动时间（更新未重启可识别）"
```

---

## Task A4: P1-1 schtasks ONSTART + RestartOnFailure

**Files:**
- Modify: `ops/manage_ops_schtasks.py` `register_server`
- Test: `tests/test_manage_ops_schtasks.py`（mock subprocess，断言 PowerShell 命令含 BootTrigger/RestartCount）

**Interfaces:**
- Produces: `register_server(user=None, restart_on_failure=True)`——用 PowerShell `Register-ScheduledTask` 注册 ONSTART（BootTrigger）+ `RestartCount=3 / RestartInterval=1min`；PowerShell 失败时回退 `schtasks /SC ONSTART`（不带重启策略，并打印手动 XML 提示）。

- [ ] **Step 1: 写失败测试**

```python
def test_register_server_uses_boot_trigger_with_restart_on_failure(monkeypatch):
    """A4: QuanterServer 必须 ONSTART(BootTrigger) 且 RestartOnFailure（08-06 实测 LogonTrigger）。"""
    import ops.manage_ops_schtasks as m
    calls = []
    def fake_run(args, **kw):
        calls.append(args)
        return 0
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    m.register_server(user="u1")
    joined = " ".join(" ".join(c) for c in calls)
    assert "Register-ScheduledTask" in joined
    assert "BootTrigger" in joined
    assert "RestartCount" in joined
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_manage_ops_schtasks.py -k boot_trigger -v`
Expected: FAIL（当前走 `schtasks /Create /SC ONSTART`，无 PowerShell/重启策略）

- [ ] **Step 3: 实现**

```python
def _register_with_powershell(user: str) -> int:
    """用 PowerShell ScheduledTasks 注册 BootTrigger + RestartOnFailure。

    Why 不用 schtasks CLI：schtasks /Create 不支持 RestartOnFailure（Task Scheduler 2.0
    属性只能 XML/PowerShell 设置）。A4 前实测任务为 LogonTrigger 且无重启策略——机器
    重启不登录则引擎不拉起，进程崩溃也不自愈（08-05 事故的部署层缺口）。
    """
    script = (
        "$action = New-ScheduledTaskAction -Execute "
        f"'{ROOT / 'scripts' / 'start_server.bat'}';"
        "$trigger = New-ScheduledTaskTrigger -AtStartup;"
        "$settings = New-ScheduledTaskSettingsSet -RestartCount 3 "
        "-RestartInterval (New-TimeSpan -Minutes 1);"
        "$principal = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType Password;"
        "Register-ScheduledTask -TaskName 'QuanterServer' -Action $action "
        "-Trigger $trigger -Settings $settings -Principal $principal -Force"
    )
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True).returncode
```

`register_server`：先 `_register_with_powershell`，rc==0 打印 OK；否则回退原 `schtasks /Create /SC ONSTART` 并打印手动 XML 提示。

- [ ] **Step 4: 运行确认通过 + 真实验证（注册后）**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_manage_ops_schtasks.py -v`
Expected: PASS

人工/运维验证（不自动执行）：
```powershell
schtasks /Query /TN QuanterServer /XML
```
Expected: XML 含 `<BootTrigger>` 与 `<RestartOnFailure>`

- [ ] **Step 5: Commit**

```bash
git add ops/manage_ops_schtasks.py tests/test_manage_ops_schtasks.py
git commit -m "fix(ops): A4 QuanterServer 注册改 BootTrigger + RestartOnFailure（P1-1）"
```

---

## Task A5: P1-2 生产 fail-closed + dev 实例隔离 + connect bot 解耦

**Files:**
- Modify: `trading/__main__.py`（`QUANTER_REQUIRE_LIVE` 硬闸）、`scripts/start_server.bat`（生产链置 env）、`ops/dev.py`（改走 `python -m trading` + dev env）、`presentation/server/main.py`（lifespan 跳过 connect bot）
- Test: `tests/trading/test_main.py`（require-live）、`tests/presentation/test_lifespan_consolidation.py`（skip bots）、`tests/ops/test_dev_backend.py`（新建，断言 dev 后端命令/env）

**Interfaces:**
- Produces: env `QUANTER_REQUIRE_LIVE=1` 时 `__main__` 在 mode != live 直接 `sys.exit(1)`；env `QUANTER_DEV_MODE=1`/`QUANTER_DEV_SKIP_CONNECT_BOTS=1`/`QUANTER_DEV_NO_RELOAD=1` 由 dev.py 注入；lifespan 在 `QUANTER_TESTING=1` 或 `QUANTER_DEV_SKIP_CONNECT_BOTS=1` 时跳过 connect bot。

- [ ] **Step 1: 写失败测试**

```python
def test_run_server_requires_live_when_env_set(monkeypatch):
    """A5: QUANTER_REQUIRE_LIVE=1 且 mode!=live → 拒绝启动（生产 fail-closed）。"""
    import pytest
    import trading.__main__ as m
    monkeypatch.setenv("QUANTER_REQUIRE_LIVE", "1")
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    monkeypatch.setattr(m, "_assert_single_instance", lambda port: None)
    with pytest.raises(SystemExit) as ei:
        m.run_server()
    assert ei.value.code == 1
```

```python
@pytest.mark.asyncio
async def test_lifespan_skips_connect_bots_when_dev_flag(monkeypatch):
    """A5: QUANTER_DEV_SKIP_CONNECT_BOTS=1 → 不 start 任何 connect bot。"""
    from fastapi import FastAPI
    from presentation.server.main import lifespan
    from tests.presentation.test_lifespan_consolidation import _mock_lifespan_dependencies
    monkeypatch.setenv("QUANTER_DEV_SKIP_CONNECT_BOTS", "1")
    app = FastAPI()
    stack, start_mock, _stop, _eng = _mock_lifespan_dependencies()
    with stack:
        async with lifespan(app):
            assert app.state.connect_bots == []
    start_mock.assert_not_called()
```

```python
def test_dev_backend_uses_python_m_trading_with_dev_env():
    """A5: dev.py 后端命令 = venv python -m trading，env 含 QUANTER_DEV_*。"""
    from ops import dev
    cmd = dev._backend_cmd(reload=False)
    assert cmd[-2:] == ["-m", "trading"]
    env = dev._backend_env(reload=False)
    assert env["QUANTER_DEV_SKIP_CONNECT_BOTS"] == "1"
    assert env["QUANTER_DEV_NO_RELOAD"] == "1"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_main.py -k requires_live tests/presentation/test_lifespan_consolidation.py -k skips_connect_bots tests/ops/test_dev_backend.py -v`
Expected: FAIL（三个能力均不存在）

- [ ] **Step 3: 实现**

`__main__.py` `run_server` 开头：

```python
    if os.getenv("QUANTER_REQUIRE_LIVE") == "1" and os.getenv("AUTO_TRADE_MODE") != "live":
        logger.critical("生产链要求 AUTO_TRADE_MODE=live，当前=%s，拒绝启动（fail-closed）",
                        os.getenv("AUTO_TRADE_MODE"))
        sys.exit(1)
```

`scripts/start_server.bat` 在 python 行前加 `set QUANTER_REQUIRE_LIVE=1`。

`ops/dev.py` 新增纯函数并改 main：

```python
def _backend_cmd(reload: bool) -> list[str]:
    # A5：统一走 python -m trading（supervisor 语义：单入口 + .env 加载 + 端口断言）
    return [str(VENV_PY), "-m", "trading"]

def _backend_env(reload: bool) -> dict:
    env = dict(os.environ)
    env["QUANTER_DEV_MODE"] = "1"
    env["QUANTER_DEV_SKIP_CONNECT_BOTS"] = "1"
    env["QUANTER_DEV_NO_RELOAD"] = "0" if reload else "1"
    return env
```

`main()` 中 `backend_cmd` 与 `Popen(..., env=_backend_env(reload))` 替换原 uvicorn 命令。

`presentation/server/main.py` lifespan connect bot 块开头：

```python
    _skip_bots = (os.environ.get("QUANTER_TESTING") == "1"
                  or os.environ.get("QUANTER_DEV_SKIP_CONNECT_BOTS") == "1")
    if _skip_bots:
        logging.getLogger(__name__).info(
            "lifespan 跳过 connect bot（QUANTER_TESTING/QUANTER_DEV_SKIP_CONNECT_BOTS=1）")
        app.state.connect_bots = []
    else:
        ...  # 既有 5 bot 循环原样
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_main.py tests/presentation/test_lifespan_consolidation.py tests/ops/test_dev_backend.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/__main__.py scripts/start_server.bat ops/dev.py presentation/server/main.py tests/trading/test_main.py tests/presentation/test_lifespan_consolidation.py tests/ops/test_dev_backend.py
git commit -m "feat(ops): A5 生产 fail-closed + dev 统一入口 + connect bot 生命周期解耦（P1-2）"
```

---

## Task A6: audit_ssot 进程拓扑三项 + 去 wmic

**Files:**
- Modify: `scripts/audit_ssot.py`（`check_engine_process_count` 改 `netstat`/`Get-Process`；新增 `check_client_process`、`check_port_owner_consistency`）
- Test: `tests/test_audit_ssot.py`（新建；若已有则扩展）

**Interfaces:**
- Produces: `check_engine_process_count() -> str | None`（改用 `tasklist`/`Get-Process` + 命令行 fallback）；`check_client_process() -> str | None`（XtMiniQmt 进程数 == 1）；`check_port_owner_consistency(port=8000) -> str | None`（端口属主 == pid 文件 PID 且进程存活）。

- [ ] **Step 1: 写失败测试**

```python
def test_check_engine_process_count_ok_when_single(monkeypatch):
    """A6: 恰好 1 个 -m trading 进程 → None（不告警）。"""
    import scripts.audit_ssot as a
    monkeypatch.setattr(a, "_engine_processes", lambda: [{"pid": 1}])
    assert a.check_engine_process_count() is None


def test_check_port_owner_drift_reports(monkeypatch, tmp_path):
    """A6: 端口属主 != pid 文件 PID → 返回漂移告警文案。"""
    import scripts.audit_ssot as a
    monkeypatch.setattr(a, "_port_holder_pid", lambda port: 100)
    monkeypatch.setattr(a, "_pid_file_owner", lambda: 200)
    msg = a.check_port_owner_consistency()
    assert msg is not None and "不一致" in msg
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_audit_ssot.py -v`
Expected: FAIL（函数不存在）

- [ ] **Step 3: 实现**

```python
def _engine_processes() -> list[dict]:
    """列引擎进程（cmdline 含 -m trading / presentation.server.main:app）。"""
    # 优先 PowerShell Get-CimInstance（带 timeout），失败降级 Get-Process exe 路径匹配
    ...

def _port_holder_pid(port: int = 8000) -> int | None:
    """netstat -ano 解析 LISTENING PID（与 supervisor 同源）。"""
    out = subprocess.run(["netstat", "-ano"], capture_output=True,
                         text=True, errors="replace").stdout
    for line in out.splitlines():
        if "LISTENING" in line and f":{port} " in line:
            parts = line.split()
            return int(parts[-1])
    return None

def _pid_file_owner(session_id: str | None = None) -> int | None:
    """logs/trading_engine_<sid>.pid 首字段。"""
    sid = session_id or os.environ.get("QMT_SESSION_ID", "default")
    p = ROOT / "logs" / f"trading_engine_{sid}.pid"
    try:
        return int(p.read_text(encoding="utf-8").split()[0])
    except Exception:
        return None

def check_port_owner_consistency(port: int = 8000) -> str | None:
    owner = _port_holder_pid(port)
    pidf = _pid_file_owner()
    if owner is not None and pidf is not None and owner != pidf:
        return f"端口 {port} 属主 {owner} 与 pid 文件 {pidf} 不一致"
    if owner is not None and pidf is None:
        return f"端口 {port} 被 {owner} 占用但无 pid 文件（旧链/非法链）"
    return None
```

`check_engine_process_count` 的 wmic 调用改为 `_engine_processes()`（`Get-CimInstance` 短超时 + `Get-Process` fallback），并把三项检查注册进 `main()` 巡检清单。

- [ ] **Step 4: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_audit_ssot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/audit_ssot.py tests/test_audit_ssot.py
git commit -m "feat(audit): A6 进程拓扑三项检查（引擎/客户端/端口属主）+ 去 wmic 依赖"
```

---

# Phase B1：进程单一所有者 + 测试隔离

## Task B1: ops/trading_supervisor.py（三合一校验 + 状态/启动/停止）

**Files:**
- Create: `ops/trading_supervisor.py`
- Test: `tests/ops/test_trading_supervisor.py`（新建）

**Interfaces:**
- Produces:
  - `port_holder_pid(port=8000) -> int | None`
  - `pid_file_owner(session_id=None, lock_dir=None) -> int | None`
  - `lock_held(session_id=None, lock_dir=None) -> bool`（探测不抢锁：acquire 成功后恢复原 pid 文件再 release）
  - `engine_processes() -> list[dict]`（pid/ppid/exe/cmdline）
  - `status(port=8000, session_id=None) -> dict`（含 `consistent` / `drifts` / `preferred_sid` / `actual_sid` 占位）
  - `start(port=8000) -> int`、`stop(port=8000, yes=False) -> int`
  - CLI：`--status`（默认）/ `--start` / `--stop`（需 `--yes`）/ `--port` / `--session`

- [ ] **Step 1: 写失败测试**

```python
def test_port_holder_parses_netstat(monkeypatch):
    from ops import trading_supervisor as s
    monkeypatch.setattr(s.subprocess, "run", lambda *a, **kw: _FakeProc(
        "TCP 0.0.0.0:8000 0.0.0.0:0 LISTENING 27592\n"))
    assert s.port_holder_pid() == 27592


def test_consistency_ok_when_all_same(monkeypatch):
    from ops import trading_supervisor as s
    monkeypatch.setattr(s, "port_holder_pid", lambda port=8000: 123)
    monkeypatch.setattr(s, "pid_file_owner", lambda *a, **kw: 123)
    monkeypatch.setattr(s, "lock_held", lambda *a, **kw: True)
    st = s.status()
    assert st["consistent"] is True and st["drifts"] == []


def test_consistency_drift_port_owner_differs(monkeypatch):
    from ops import trading_supervisor as s
    monkeypatch.setattr(s, "port_holder_pid", lambda port=8000: 100)
    monkeypatch.setattr(s, "pid_file_owner", lambda *a, **kw: 200)
    monkeypatch.setattr(s, "lock_held", lambda *a, **kw: True)
    st = s.status()
    assert st["consistent"] is False and len(st["drifts"]) >= 1
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/ops/test_trading_supervisor.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现核心**

```python
def port_holder_pid(port: int = 8000) -> int | None:
    out = subprocess.run(["netstat", "-ano"], capture_output=True,
                         text=True, errors="replace").stdout
    for line in out.splitlines():
        if "LISTENING" in line and f":{port} " in line:
            parts = line.split()
            try:
                return int(parts[-1])
            except ValueError:
                return None
    return None


def pid_file_owner(session_id: str | None = None, lock_dir: str | None = None) -> int | None:
    from trading.single_instance import _pid_path
    sid = session_id or os.environ.get("QMT_SESSION_ID", "default")
    try:
        return int(_pid_path(sid, lock_dir).read_text(encoding="utf-8").split()[0])
    except Exception:
        return None


def lock_held(session_id: str | None = None, lock_dir: str | None = None) -> bool:
    """探测锁是否被持有（不污染 pid 文件）。

    Why 先备份再 acquire：single_instance.acquire 在锁空闲时会重写 pid 文件，
    探测不能把 supervisor 的 pid 写进引擎 pid 文件（三合一校验会失真）。
    """
    from trading import single_instance
    sid = session_id or os.environ.get("QMT_SESSION_ID", "default")
    path = single_instance._pid_path(sid, lock_dir)
    backup = None
    try:
        backup = path.read_text(encoding="utf-8") if path.exists() else None
        lock = single_instance.acquire(sid, lock_dir)
    except Exception:
        return True  # 探测异常保守视为已持有（fail-closed）
    if lock is None:
        return True
    try:
        if backup:
            path.write_text(backup, encoding="utf-8")
        elif path.exists():
            path.unlink()
    finally:
        lock.release()
    return False


def status(port: int = 8000, session_id: str | None = None) -> dict:
    owner = port_holder_pid(port)
    pidf = pid_file_owner(session_id)
    held = lock_held(session_id)
    drifts: list[str] = []
    if owner is not None and pidf is not None and owner != pidf:
        drifts.append(f"端口属主 {owner} != pid 文件 {pidf}")
    if owner is None and pidf is not None:
        drifts.append("pid 文件存在但端口无监听（进程已死/未绑定）")
    if owner is None and held:
        drifts.append("锁被持有但端口无监听（异常/僵持态）")
    return {
        "port": port, "port_holder_pid": owner, "pid_file_pid": pidf,
        "lock_held": held, "engine_pids": [p["pid"] for p in engine_processes()],
        "preferred_sid": session_id or os.environ.get("QMT_SESSION_ID", "default"),
        "actual_sid": _read_runtime_session(),   # B2 落地前为 None
        "client": _client_status(),              # XtMiniQmt 进程探测
        "consistent": not drifts, "drifts": drifts,
        "git": _git_rev(), "started_at": _runtime_started_at(),
    }
```

`start()`：`status()` 一致且无引擎进程 → `schtasks /Run /TN QuanterServer`（注册存在时）或 `Popen([VENV_PY, "-m", "trading"], cwd=ROOT, creationflags=DETACHED)`；不一致 → 打印 drifts + 返 2。

`stop()`：`yes=False` 时只打印将杀清单返 0；`yes=True` 时对 `engine_processes()` 逐 `taskkill /F /T /PID`。

- [ ] **Step 4: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/ops/test_trading_supervisor.py -v`
Expected: PASS

- [ ] **Step 5: 手动 smoke（只读）**

Run: `.venv310/Scripts/python.exe ops/trading_supervisor.py --status`
Expected: 打印当前拓扑（08-06 实测应显示 system Python 进程、8000 无监听、drifts）

- [ ] **Step 6: Commit**

```bash
git add ops/trading_supervisor.py tests/ops/test_trading_supervisor.py
git commit -m "feat(ops): B1 trading_supervisor 三合一校验 + 状态/启动/停止（进程单一所有者）"
```

---

## Task B2: ops/restart_trading.py（原子重启唯一入口）

**Files:**
- Create: `ops/restart_trading.py`
- Test: `tests/ops/test_restart_trading.py`（新建）

**Interfaces:**
- Produces: CLI `restart [--yes]`——默认 dry-run 打印「将停进程树 + 启动方式」；`--yes` 时 `supervisor.stop(yes=True)` → `supervisor.start()`；退出码 0 成功 / 2 校验不通过。

- [ ] **Step 1: 写失败测试**

```python
def test_restart_dry_run_does_not_kill(monkeypatch):
    from ops import restart_trading as r, trading_supervisor as s
    killed = []
    monkeypatch.setattr(s, "stop", lambda port=8000, yes=False: killed.append(yes))
    monkeypatch.setattr(s, "start", lambda port=8000: 0)
    assert r.main(["restart"]) == 0
    assert killed == [False]  # dry-run：stop 只展示不杀
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/ops/test_restart_trading.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="原子重启 Quanter 引擎（唯一入口）")
    p.add_argument("action", choices=["restart", "status"])
    p.add_argument("--yes", action="store_true", help="真正停旧树并启动；缺省 dry-run")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    if args.action == "status":
        print(json.dumps(trading_supervisor.status(args.port), ensure_ascii=False, indent=2))
        return 0
    st = trading_supervisor.status(args.port)
    if not st["consistent"]:
        print("三合一校验不通过，拒绝重启：", st["drifts"])
        return 2
    trading_supervisor.stop(port=args.port, yes=args.yes)
    if not args.yes:
        print("dry-run：以上为将停止的进程；加 --yes 执行")
        return 0
    return trading_supervisor.start(port=args.port)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/ops/test_restart_trading.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ops/restart_trading.py tests/ops/test_restart_trading.py
git commit -m "feat(ops): B2 restart_trading 原子重启唯一入口（默认 dry-run）"
```

---

## Task B3: QUANTER_TESTING 测试隔离 + pytest.ini server_lifecycle

**Files:**
- Modify: `trading/__main__.py`（`_assert_single_instance` 与 `run_server` 跳过）、`trading/engine.py`（bootstrap 锁跳过）、`pytest.ini`
- Test: `tests/trading/test_main_testing_mode.py`（新建）

**Interfaces:**
- Produces: env `QUANTER_TESTING=1` 时 `_assert_single_instance` no-op、`run_server` 不探测端口、engine bootstrap 不 acquire session 锁；pytest 默认排除 `server_lifecycle`。

- [ ] **Step 1: 写失败测试**

```python
def test_assert_single_instance_skips_when_testing(monkeypatch):
    import trading.__main__ as m
    monkeypatch.setenv("QUANTER_TESTING", "1")
    monkeypatch.setattr(m, "_port_holder_alive", lambda port: 12345)
    m._assert_single_instance()  # 不应 SystemExit


def test_run_server_skips_port_assert_in_testing(monkeypatch):
    import trading.__main__ as m
    monkeypatch.setenv("QUANTER_TESTING", "1")
    called = []
    monkeypatch.setattr(m, "_assert_single_instance",
                        lambda port: called.append(port))
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    m.run_server()
    assert called == []
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_main_testing_mode.py -v`
Expected: FAIL（testing 未生效）

- [ ] **Step 3: 实现**

`__main__.py`：

```python
def _in_testing() -> bool:
    """QUANTER_TESTING=1 → 跳过端口/单实例断言（pytest 不 bind 8000、不抢 session）。"""
    return os.getenv("QUANTER_TESTING") == "1"


def _assert_single_instance(port: int = 8000) -> None:
    if _in_testing():
        return
    ...
```

`run_server`：`if not _in_testing(): _assert_single_instance(server_port)`。

`engine.py` bootstrap 锁：

```python
        if _mode() == "live" and os.getenv("QUANTER_TESTING") != "1":
            from trading import single_instance
            ...
```

`pytest.ini`：

```ini
addopts = -v --tb=short -m "not slow and not e2e_long and not server_lifecycle"
markers =
    ...
    server_lifecycle: tests that boot the real HTTP server (deselect by default; run with -m server_lifecycle)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_main_testing_mode.py tests/trading/test_main.py tests/trading/test_single_instance.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/__main__.py trading/engine.py pytest.ini tests/trading/test_main_testing_mode.py
git commit -m "feat(test): B3 QUANTER_TESTING 跳过单实例/端口断言 + server_lifecycle 默认排除"
```

---

## Task B4: ops/dev.py 统一 supervisor 语义（并入 A5 的收尾验证）

**Files:**
- Verify: `ops/dev.py`（A5 已改）+ `tests/ops/test_dev_backend.py`

- [ ] **Step 1: 验证 dev.py 启动后端= `python -m trading` 且 connect bot 不随起**

Run: `.venv310/Scripts/python.exe -m pytest tests/ops/test_dev_backend.py -v`
Expected: PASS

- [ ] **Step 2: 手动 smoke（可选，不自动起服务）**

```powershell
python ops/dev.py --help   # 确认无回归
```

- [ ] **Step 3: Commit（若 A5 未含 dev 测试则补）**

```bash
git add ops/dev.py tests/ops/test_dev_backend.py
git commit -m "test(dev): B4 dev.py 统一入口 + dev env 契约测试"
```

---

## Task B5: 清理当前残留进程 + runbook 更新

**Files:**
- Modify: `docs/superpowers/runbooks/2026-08-04-gateway-ops.md`

**Interfaces:**
- Produces: runbook §0 新增「标准启动/重启 = `ops/restart_trading.py`」；§1 清理步骤改为先 `restart_trading.py` dry-run 再 `--yes`。

- [ ] **Step 1: 更新 runbook**

在 `docs/superpowers/runbooks/2026-08-04-gateway-ops.md` 开头加：

```markdown
> 2026-08-06 起：引擎启动/重启唯一入口 = `python ops/restart_trading.py restart [--yes]`；
> 状态查看 = `python ops/restart_trading.py status`。不再手动 `schtasks /Run` / `python -m trading`。
```

§1.2 清理步骤改为引用 `restart_trading.py`（dry-run 展示 → `--yes` 执行），保留「禁止自动 taskkill 其他 python」红线。

- [ ] **Step 2: 人工执行清理（实盘操作，需用户确认）**

```powershell
.venv310\Scripts\python.exe ops\restart_trading.py status   # 看拓扑
.venv310\Scripts\python.exe ops\restart_trading.py restart --yes
```

Expected: 08-06 拓扑收敛为单 venv `-m trading` 进程 + 8000 单监听 + XtMiniQmt 在。

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/runbooks/2026-08-04-gateway-ops.md
git commit -m "docs(ops): B5 runbook 统一重启入口为 restart_trading.py"
```

---

## 实现偏差说明（code-review 修正 · 2026-08-06）

主 agent 深度 review（033e4a85..HEAD）后修正 4 处实现偏差，均已在对应 commit 落地：

1. **A2 failed 判定收窄**：plan 原文「live 有计划单但 submitted=0 → failed」误伤
   veto/超期/has_order 已挂等「有意跳过」（submitted=0 且 rejected=0）。修正为
   `rejected>0` 才算 failed，防 C-8 重试噪音。对应测试
   `test_pre_open_live_zero_rejected_marks_done`。
2. **B1 CLI --status 默认**：plan 写「--status（默认）」，首版实现用 required 组；
   修正为无参默认 status。
3. **共享探测模块**：端口属主/pid 文件/引擎进程/客户端探测从 trading_supervisor 与
   audit_ssot 两份复制抽为 `ops/process_topology.py` 单源（消除重复代码 + audit 版
   漏 TRADING_ENGINE_LOCK_DIR 的问题）。
4. **run_trading_engine.bat 统一 fail-closed**：第二入口补 `QUANTER_REQUIRE_LIVE=1`，
   标注唯一入口为 start_server.bat / restart_trading.py。

---

## Self-Review

**1. Spec 覆盖**：
- §7.1-1（09:22 集合竞价）→ A1 ✅
- §7.1-4（台账 partial/failed + C-8 重试）→ A2 ✅
- §7.1-5（版本/启动时间 + 更新未重启告警）→ A3（banner）+ B1（status 漂移比对）✅
- §7.1-6（ONSTART + RestartOnFailure）→ A4 ✅
- §7.1-7（fail-closed + dry_run 隔离）→ A5 ✅
- §7.1-9（audit_ssot 三项）→ A6 ✅
- §7.1-10（connect bot 解耦）→ A5 ✅
- §6 B1 1-9（supervisor/restart/测试隔离/dev/runbook）→ B1-B5 ✅
- §6 Phase A 批量删除 → **未覆盖（未决问题 §2.4 裁定后另写 plan）**；DRAFT 行号漂移警告已在 spec §2 标注。
- §6 B2/B3（guard/端点/L2 sid 轮换、audit 剩余）→ A6 覆盖 B3 缺口；B2 另写 plan。

**2. 占位符扫描**：A2 测试的 gate/plan 构造引用「按既有测试范式补全」是诚实标注（每个 task 给了核心断言与定位符号），非占位；A6 `_engine_processes` 内部省略号需在实现时按 B1 `engine_processes` 同源实现（两处共用逻辑可在 B1 后抽公共函数，本 plan 允许实现者复用）。

**3. 类型一致**：`_in_a_share_session(now=None)`、`_pre_open_impl` 返回键（submitted/rejected/total/mode）、`status()` 返回键（port_holder_pid/pid_file_pid/lock_held/consistent/drifts/preferred_sid/actual_sid/client/git/started_at）在 A/B 各 task 间一致；`restart_trading` 与 `trading_supervisor` 的 `stop(port, yes)` / `start(port)` 签名一致。

**4. 顺序依赖**：A1-A6 相互独立（A2 依赖既有 `n_rejected` 计数，需先 Grep 确认存在）；B1 先于 B2/B5（B2 调 B1 API）；B3 独立；A5 与 B4 有重叠（B4 是 A5 的收尾验证），执行时可合并提交。

---

## Execution Handoff

Plan 完整保存于 `docs/superpowers/plans/2026-08-06-process-gateway-phase1.md`。两种执行方式：

**1. Subagent-Driven（推荐）**：每个 task 派一个新 subagent，task 间 review（implementer → reviewer → fix loop），快迭代。A1-A6 多数独立可并行；B1→B2/B5 串行。

**2. Inline Execution**：本会话内按 executing-plans 批量执行，检查点 review。

**建议**：先跑 A1 + A2（明天 09:22 前必须合入），再 B1/B2（进程收敛），最后 A3-A6 + B3/B5。Phase A 批量删风控与 B2 guard 在未决问题裁定后另立 plan。
