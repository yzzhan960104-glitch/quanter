# C-7 start_all 收编 + discovery 启动补跑 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** start_all 收编（broadcast connect + discovery 进 uvicorn lifespan）+ schtasks ONSTART 删 start_all.py + discovery 启动补跑（offline 容错，收编自洽必需）。

**Architecture:** lifespan 装 connect（5 CONNECT_BOTS start/stop 软降级）+ discovery cron 02:00（engine.sched add_job → subprocess `python -m discovery daemon` 子进程隔离，复用 cli 装配）+ discovery 启动补跑（lifespan startup 检查 search_run 最新 started_at，错过昨晚 02:00 则异步 subprocess 补跑，幂等）。schtasks ONSTART 起 `python -m trading`（session 0 后台，删 start_all.py）。

**Tech Stack:** Python 3.10、FastAPI/uvicorn、APScheduler（engine.sched AsyncIOScheduler）、subprocess、Windows schtasks、pytest。

## Global Constraints

- **全中文注释**（CLAUDE.md，What + Why）。
- **connect/discovery 进 lifespan 软降级**：装配失败不阻断 uvicorn（独立 try/except，同 engine/training_orchestrator 范式）。
- **discovery 启动补跑幂等**：discovery 既有轮次/seed 派生（[[discovery-engine-status]]）+ run_daemon_cycle 早退（status==converged 跳过），补跑+当晚 02:00 双跑靠此去重。
- **schtasks ONSTART 删 start_all.py**：`/SC ONSTART /TR scripts\start_server.bat /RU <user>`（凭证 plan V4 定：倾向用户+密码，venv/.env 可靠）。
- **退历史 schtasks**：discovery QuanterDiscoveryDaemon（`discovery.schtasks --unregister`）+ pipeline/brief（`manage_ops_schtasks --unregister-pipeline-brief`）。
- **不动 C-4/C-5/C-6**：_critical_guard/_health_guard(C-4 不升 L1)/_gw_health_gate(C-5)/clock(C-6) 不变。
- **push schtasks 不动**：trading/data/strategy push bot 的 schtasks 保留（C-7 只收编 connect/discovery）。
- **全量回归基线**：C-6 后 **1164 passed / 0 failed**，本期零退化。
- **测试入口**：`F:/quanter/.venv310/Scripts/python.exe -m pytest ...`（简写 pytest）。
- **commit 规范**：`feat(c7-vN): ...` / `fix(c7-vN): ...`，结尾 `Co-Authored-By: Claude <noreply@anthropic.com>`。

## File Structure

| 文件 | 责任 | 本期改动 |
|---|---|---|
| `presentation/server/main.py` | FastAPI lifespan | **V1/V2/V3**：lifespan 装 connect（5 CONNECT_BOTS start/stop）+ discovery cron 02:00（engine.sched add_job → subprocess）+ discovery 启动补跑（_discovery_missed_last_run + 异步补跑）。 |
| `tests/presentation/test_lifespan_consolidation.py` | lifespan 收编单测（新建） | **V1/V2/V3**：mock connect_manager.start/stop + mock sched.add_job + mock subprocess + _discovery_missed_last_run 两态。 |
| `ops/manage_ops_schtasks.py` | schtasks 管理 | **V4**：加 `--register-server`（注册 QuanterServer ONSTART）+ `--unregister-discovery`（退 QuanterDiscoveryDaemon）；register() 兜底退 discovery。 |
| `scripts/start_server.bat` | schtasks ONSTART 入口（新建） | **V4**：`cd /d F:\quanter && .venv310\Scripts\python.exe -m trading`。 |
| `ops/start_all.py` + `scripts/start_all.bat` | 旧全栈编排 | **V4 删**（职责收编 lifespan + schtasks ONSTART）。 |
| `tests/test_manage_ops_schtasks.py` | schtasks 单测 | **V4**：加 `--register-server` / `--unregister-discovery` 命令单测（如文件不存在则新建）。 |

---

## Task 1 (V1)：broadcast connect 进 lifespan

**Files:**
- Modify: `presentation/server/main.py`（lifespan startup 加 connect 装配块 + shutdown 加 stop 块）
- Test: `tests/presentation/test_lifespan_consolidation.py`（新建）

**Interfaces:**
- Consumes: `broadcast.__main__.CONNECT_BOTS`（5 bot dict）、`CONNECT_DEFAULTS`、`broadcast.connect_manager.start(bot,cfg,defaults)` / `stop(bot)`（既有）。
- Produces: `app.state.connect_bots`（list，已起的 bot，供 shutdown stop）。

- [ ] **Step 1：写失败测试（新建 test_lifespan_consolidation.py）**

创建 `tests/presentation/test_lifespan_consolidation.py`：

```python
# -*- coding: utf-8 -*-
"""C-7 V1：lifespan 装 broadcast connect（5 CONNECT_BOTS start/stop 软降级）。

物理意图（spec §3.1）：start_all step ② connect 编排收编进 lifespan，软降级
（单 bot 失败不阻断 uvicorn）。live reload=False（C-5 V1）不 reload，connect 不抖动。
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock


def test_lifespan_starts_connect_bots_on_startup():
    """lifespan startup 遍历 CONNECT_BOTS 调 connect_manager.start（软降级单 bot 失败跳过）。"""
    from presentation.server.main import lifespan
    from fastapi import FastAPI

    app = FastAPI()
    started_bots = []

    def _fake_start(bot, cfg, defaults):
        started_bots.append(bot)
        return "started"

    with patch("broadcast.connect_manager.start", side_effect=_fake_start), \
         patch("broadcast.connect_manager.stop") as stop_mock, \
         patch("presentation.server.main.build_default_manager"), \
         patch("data.lake_reader.DataLakeReader.get_instance"), \
         patch("presentation.server.main.TradingEngine") as Eng:
        # 简化：mock 掉 lifespan 其它重装配（engine/replay/training），聚焦 connect
        Eng.return_value.sched.add_job = MagicMock()
        Eng.return_value.bootstrap = MagicMock(return_value=__import__("asyncio").coroutine(lambda: None)())
        Eng.return_value.start = MagicMock()
        Eng.return_value.shutdown = MagicMock()
        import asyncio
        asyncio.run(lifespan_startup_only(app))

    # 验 5 CONNECT_BOTS 都调 start
    from broadcast.__main__ import CONNECT_BOTS
    assert set(started_bots) == set(CONNECT_BOTS.keys())
    assert app.state.connect_bots == list(CONNECT_BOTS.keys())


def test_lifespan_connect_soft_degrade_on_single_bot_failure():
    """单 bot start 抛 RuntimeError → 跳过该 bot，其余正常起，不阻断 uvicorn。"""
    import asyncio
    from presentation.server.main import lifespan_startup_only
    from fastapi import FastAPI

    app = FastAPI()
    started = []

    def _fake_start(bot, cfg, defaults):
        if bot == "review":
            raise RuntimeError("配置缺失")
        started.append(bot)
        return "started"

    with patch("broadcast.connect_manager.start", side_effect=_fake_start), \
         patch("presentation.server.main.build_default_manager"), \
         patch("data.lake_reader.DataLakeReader.get_instance"), \
         patch("presentation.server.main.TradingEngine") as Eng:
        Eng.return_value.sched.add_job = MagicMock()
        asyncio.run(lifespan_startup_only(app))

    from broadcast.__main__ import CONNECT_BOTS
    assert "review" not in started  # 失败 bot 跳过
    assert set(started) == set(CONNECT_BOTS.keys()) - {"review"}  # 其余 4 正常
    assert "review" not in app.state.connect_bots


def test_lifespan_stops_connect_bots_on_shutdown():
    """lifespan shutdown 遍历 app.state.connect_bots 调 connect_manager.stop（树杀）。"""
    import asyncio
    from presentation.server.main import lifespan_shutdown_only
    from fastapi import FastAPI

    app = FastAPI()
    app.state.connect_bots = ["cli", "trading_q", "data_q", "strategy_q", "review"]
    stopped = []
    with patch("broadcast.connect_manager.stop", side_effect=lambda b: stopped.append(b)):
        asyncio.run(lifespan_shutdown_only(app))
    assert set(stopped) == {"cli", "trading_q", "data_q", "strategy_q", "review"}
```

**注**：上述测试假设 main.py 抽出 `lifespan_startup_only(app)` / `lifespan_shutdown_only(app)` 两个可独立调的函数（便于单测），或测试直接调 lifespan 上下文。implementer 按 main.py 实际 lifespan 结构调整（若 lifespan 是 @asynccontextmanager，测试用 `_run_lifespan(app)` helper 跑 startup/shutdown 段）。

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/presentation/test_lifespan_consolidation.py -v`
Expected: FAIL（main.py 未装 connect / `lifespan_startup_only` 未抽出）。

- [ ] **Step 3：实现——lifespan 加 connect 装配 + shutdown stop**

Edit `presentation/server/main.py` lifespan：

**3a. lifespan startup**（TradingEngine 装配块后，`yield` 前）加 connect 装配块：

```python
    # C-7 V1：broadcast connect 收编进 lifespan（5 CONNECT_BOTS）。
    # 物理意图（spec §3.1）：start_all step ② connect 编排移此处，软降级（单 bot
    # 失败不阻断 uvicorn）。live reload=False（C-5 V1）不 reload，connect 不抖动。
    try:
        from broadcast.__main__ import CONNECT_BOTS, CONNECT_DEFAULTS
        from broadcast import connect_manager
        started_bots = []
        for _bot in CONNECT_BOTS:  # cli/trading_q/data_q/strategy_q/review
            try:
                connect_manager.start(_bot, CONNECT_BOTS[_bot], CONNECT_DEFAULTS)
                started_bots.append(_bot)
            except RuntimeError:
                # 配置缺失（unified_app_id 未填等）→ 跳过该 bot，同 _connect_start 语义
                logging.getLogger(__name__).warning(
                    "connect bot=%s 配置缺失跳过", _bot, exc_info=True)
            except Exception:
                logging.getLogger(__name__).exception(
                    "connect bot=%s 起异常（跳过，不阻断 uvicorn）", _bot)
        app.state.connect_bots = started_bots
    except Exception:
        logging.getLogger(__name__).exception("lifespan 装 connect 异常（已忽略）")
        app.state.connect_bots = []
```

**3b. lifespan shutdown**（断网关后，scheduler stop 前）加 connect stop 块：

```python
    # C-7 V1：shutdown 树杀 connect bots（与 startup start 对偶）。
    for _bot in getattr(app.state, "connect_bots", []):
        try:
            from broadcast import connect_manager
            connect_manager.stop(_bot)  # taskkill /F /T 树杀
        except Exception:
            logging.getLogger(__name__).exception(
                "shutdown connect bot=%s 异常（已忽略）", _bot)
```

- [ ] **Step 4：跑测试验证通过**

Run: `pytest tests/presentation/test_lifespan_consolidation.py -v`
Expected: 3 用例 PASS（若测试假设的 `lifespan_startup_only` 与 main.py 结构不符，调整测试为调 lifespan @asynccontextmanager 的标准范式——`async with lifespan(app): pass` 跑完 startup+shutdown，断言 mock 调用）。

- [ ] **Step 5：commit**

```bash
git add presentation/server/main.py tests/presentation/test_lifespan_consolidation.py
git commit -m "feat(c7-v1): broadcast connect 进 lifespan（5 CONNECT_BOTS start/stop 软降级）

- lifespan startup 遍历 CONNECT_BOTS 调 connect_manager.start（配置缺失/异常跳过不阻断）
- lifespan shutdown 遍历 connect_bots 调 connect_manager.stop（树杀）
- 软降级 try/except（装配失败不阻断 uvicorn）
- 新建 test_lifespan_consolidation.py（3 用例：5 bots start / 单 bot 失败软降级 / shutdown stop）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2 (V2)：discovery 进 lifespan cron 02:00（subprocess 子进程）

**Files:**
- Modify: `presentation/server/main.py`（lifespan 加 discovery cron 注册）
- Modify: `tests/presentation/test_lifespan_consolidation.py`（加 discovery cron 测试）

**Interfaces:**
- Consumes: `trading.engine.TradingEngine.sched`（既有 AsyncIOScheduler，lifespan 装的 engine 实例）；`discovery.schtasks.unregister`（既有，V4 退 schtasks）。
- Produces: `_run_discovery_subprocess()`（模块级函数，cron job + V3 补跑共用）；engine.sched 加 `id="discovery_daemon"` job。

- [ ] **Step 1：写失败测试（追加 test_lifespan_consolidation.py）**

```python
def test_lifespan_registers_discovery_cron_02():
    """lifespan startup 在 engine.sched 加 discovery_daemon cron 02:00 job。"""
    import asyncio
    from presentation.server.main import lifespan_startup_only
    from fastapi import FastAPI

    app = FastAPI()
    with patch("presentation.server.main.build_default_manager"), \
         patch("data.lake_reader.DataLakeReader.get_instance"), \
         patch("presentation.server.main.TradingEngine") as Eng, \
         patch("broadcast.connect_manager.start"), \
         patch("presentation.server.main._discovery_missed_last_run", return_value=False):
        added_jobs = []
        Eng.return_value.sched.add_job = lambda func, trigger=None, **kw: added_jobs.append(kw.get("id"))
        asyncio.run(lifespan_startup_only(app))
    assert "discovery_daemon" in added_jobs  # cron 02:00 job 注册


def test_run_discovery_subprocess_detached():
    """_run_discovery_subprocess 起 DETACHED 子进程 `python -m discovery daemon`。"""
    from presentation.server.main import _run_discovery_subprocess
    with patch("presentation.server.main.subprocess.Popen") as popen:
        _run_discovery_subprocess()
    popen.assert_called_once()
    cmd = popen.call_args[0][0]
    assert "-m" in cmd and "discovery" in cmd and "daemon" in cmd
    # DETACHED 标志（creationflags 含 DETACHED_PROCESS）
    flags = popen.call_args[1].get("creationflags", 0)
    assert flags & 0x00000008  # DETACHED_PROCESS = 0x8
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/presentation/test_lifespan_consolidation.py::test_lifespan_registers_discovery_cron_02 tests/presentation/test_lifespan_consolidation.py::test_run_discovery_subprocess_detached -v`
Expected: FAIL（`_run_discovery_subprocess` 未定义 / discovery cron 未注册）。

- [ ] **Step 3：实现——_run_discovery_subprocess + engine.sched add_job discovery cron**

**3a. main.py 顶部（import 区）加 discovery 调度辅助**：

```python
# C-7 V2：discovery daemon cron 调度（subprocess 子进程隔离，复用 cli daemon 装配）。
import subprocess as _subprocess

# Windows DETACHED 标志（同 ops/start_all.py 既有范式）
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008


def _run_discovery_subprocess() -> None:
    """discovery daemon cron job：subprocess 起 `python -m discovery daemon`（子进程隔离）。

    物理意图（spec §3.2）：discovery 从 schtasks 收编 lifespan（engine.sched cron 02:00）。
    用 subprocess 而非 ProcessPoolExecutor：(1) 复用 cli cmd_daemon 装配（freeze/split/
    默认参数，无需重写）；(2) 子进程隔离（discovery 重型全市场扫描不阻塞 uvicorn 事件循环）；
    (3) 与 schtasks 当前调 run_daemon.bat 等价（行为不变）。
    DETACHED：独立进程组，uvicorn 退出不杀 discovery 子进程（夜跑长任务不依附 server）。
    """
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    _venv_py = _root / ".venv310" / "Scripts" / "python.exe"
    _log_dir = _root / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_fh = (_log_dir / "discovery_cron.log").open("a", encoding="utf-8")
    _subprocess.Popen(
        [str(_venv_py), "-m", "discovery", "daemon"],
        cwd=str(_root),
        stdout=_log_fh,
        stderr=_subprocess.STDOUT,
        stdin=_subprocess.DEVNULL,
        creationflags=_CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS,
        close_fds=True,
    )
```

**3b. lifespan startup**（connect 装配后，yield 前）加 discovery cron 注册：

```python
    # C-7 V2：discovery 收编 lifespan（engine.sched cron 02:00 → subprocess 子进程）。
    # 物理意图（spec §3.2）：discovery 从 schtasks DAILY 02:00 收编到 engine.sched
    # AsyncIOScheduler，触发 _run_discovery_subprocess（DETACHED 子进程跑 cli daemon）。
    # 软降级：engine 未装配（影子/开发）或 add_job 失败 → 跳过，不阻断 uvicorn。
    try:
        _eng = getattr(app.state, "trading_engine", None)
        if _eng is not None:
            from apscheduler.triggers.cron import CronTrigger
            _eng.sched.add_job(
                _run_discovery_subprocess,
                CronTrigger(hour=2, minute=0),
                id="discovery_daemon",
                replace_existing=True,
            )
            logging.getLogger(__name__).info("discovery cron 02:00 已注册到 engine.sched")
    except Exception:
        logging.getLogger(__name__).exception("lifespan 装 discovery cron 异常（已忽略）")
```

- [ ] **Step 4：跑测试验证通过**

Run: `pytest tests/presentation/test_lifespan_consolidation.py -v`
Expected: 全 PASS（含 V1 的 3 + V2 的 2 = 5 用例）。

- [ ] **Step 5：commit**

```bash
git add presentation/server/main.py tests/presentation/test_lifespan_consolidation.py
git commit -m "feat(c7-v2): discovery 进 lifespan cron 02:00（subprocess 子进程隔离）

- _run_discovery_subprocess() 起 DETACHED \`python -m discovery daemon\`（复用 cli 装配）
- engine.sched add_job discovery_daemon cron 02:00（subprocess 触发，不阻塞事件循环）
- 软降级（engine 未装/add_job 失败不阻断 uvicorn）
- subprocess 比 ProcessPoolExecutor 更简（复用 cli 默认参数 + 与 schtasks 同语义）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3 (V3)：discovery 启动补跑（offline 容错）

**Files:**
- Modify: `presentation/server/main.py`（lifespan 加 `_discovery_missed_last_run` + startup 补跑）
- Modify: `tests/presentation/test_lifespan_consolidation.py`（加补跑测试）

**Interfaces:**
- Consumes: `discovery.store.connect` / `search_run` 表（读最新 started_at）；`_run_discovery_subprocess`（V2 产出）。
- Produces: `_discovery_missed_last_run() -> bool`（lifespan startup 调）。

- [ ] **Step 1：写失败测试（追加）**

```python
def test_discovery_missed_last_run_true_when_no_record():
    """无 search_run 记录 → 返 True（补跑）。"""
    from presentation.server.main import _discovery_missed_last_run
    with patch("discovery.store.connect") as conn_ctx:
        conn_ctx.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
        assert _discovery_missed_last_run() is True


def test_discovery_missed_last_run_true_when_stale():
    """最新 started_at < 昨日 02:00 → 返 True（offline 跨过，补跑）。"""
    from datetime import datetime, timedelta
    from presentation.server.main import _discovery_missed_last_run
    stale = (datetime.now() - timedelta(days=2)).isoformat()
    fake_row = {"started_at": stale}
    with patch("discovery.store.connect") as conn_ctx:
        conn_ctx.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = fake_row
        assert _discovery_missed_last_run() is True


def test_discovery_missed_last_run_false_when_recent():
    """最新 started_at ≥ 昨日 02:00 → 返 False（昨晚跑了，不补跑）。"""
    from datetime import datetime, timedelta
    from presentation.server.main import _discovery_missed_last_run
    recent = (datetime.now() - timedelta(hours=2)).isoformat()  # 今天刚跑
    fake_row = {"started_at": recent}
    with patch("discovery.store.connect") as conn_ctx:
        conn_ctx.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = fake_row
        assert _discovery_missed_last_run() is False


def test_lifespan_catchup_runs_discovery_when_missed():
    """lifespan startup：_discovery_missed_last_run=True → 调 _run_discovery_subprocess 补跑。"""
    import asyncio
    from presentation.server.main import lifespan_startup_only
    from fastapi import FastAPI

    app = FastAPI()
    with patch("presentation.server.main.build_default_manager"), \
         patch("data.lake_reader.DataLakeReader.get_instance"), \
         patch("presentation.server.main.TradingEngine") as Eng, \
         patch("broadcast.connect_manager.start"), \
         patch("presentation.server.main._discovery_missed_last_run", return_value=True), \
         patch("presentation.server.main._run_discovery_subprocess") as run_disc:
        Eng.return_value.sched.add_job = MagicMock()
        asyncio.run(lifespan_startup_only(app))
    run_disc.assert_called_once()  # 补跑触发
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/presentation/test_lifespan_consolidation.py -v -k missed_or_catchup`
Expected: FAIL（`_discovery_missed_last_run` 未定义 / 补跑未触发）。

- [ ] **Step 3：实现——_discovery_missed_last_run + lifespan startup 补跑**

**3a. main.py 加 `_discovery_missed_last_run`**（在 `_run_discovery_subprocess` 旁）：

```python
def _discovery_missed_last_run() -> bool:
    """检查 discovery 是否错过昨晚 02:00（offline 容错补跑判定）。

    物理意图（spec §3.3）：生产机不 7x24，offline 跨 02:00 则当晚 discovery 漏跑
    （策略迭代断链）。本函数读 search_run 表最新 started_at（不按 snapshot_hash 过滤，
    避免 freeze 重型），与昨日 02:00 比——错过则 lifespan startup 触发补跑。

    幂等：discovery run_daemon_cycle 早退（status==converged 跳过）+ 轮次/seed 派生，
    补跑 + 当晚 02:00 双跑靠此去重（[[discovery-engine-status]]）。

    Returns:
      True = 错过（无记录 / 最新 started_at < 昨日 02:00）→ 补跑；
      False = 昨晚跑了 → 不补跑。
    """
    from datetime import datetime, timedelta
    from discovery.store import DEFAULT_DB_PATH
    import os as _os
    import sqlite3 as _sqlite3

    db_path = _os.environ.get("DISCOVERY_DB", DEFAULT_DB_PATH)
    try:
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            "SELECT started_at FROM search_run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except Exception:
        # DB 不存在 / 表未建（首次运行）→ 视为错过（补跑，让 daemon 首次跑）
        return True
    if row is None:
        return True  # 无记录
    last_started = row["started_at"]
    try:
        last_dt = datetime.fromisoformat(last_started)
    except (ValueError, TypeError):
        return True  # 时间解析失败保守补跑
    now = datetime.now()
    yesterday_02 = (now - timedelta(days=1)).replace(hour=2, minute=0, second=0, microsecond=0)
    return last_dt < yesterday_02
```

**3b. lifespan startup**（discovery cron 注册后，yield 前）加补跑：

```python
    # C-7 V3：discovery 启动补跑（offline 容错，收编自洽必需）。
    # 物理意图（spec §3.3）：offline 跨昨晚 02:00 → 启动补跑（DETACHED subprocess，
    # 不阻塞 uvicorn 起）。幂等靠 discovery 既有轮次/seed + run_daemon_cycle 早退。
    try:
        if _discovery_missed_last_run():
            logging.getLogger(__name__).warning(
                "discovery 启动补跑：检测到 offline 跨昨晚 02:00，异步补跑")
            _run_discovery_subprocess()  # DETACHED 子进程，立即返不阻塞
    except Exception:
        logging.getLogger(__name__).exception("discovery 启动补跑异常（不阻断 uvicorn）")
```

- [ ] **Step 4：跑测试验证通过**

Run: `pytest tests/presentation/test_lifespan_consolidation.py -v`
Expected: 全 PASS（V1 3 + V2 2 + V3 4 = 9 用例）。

- [ ] **Step 5：commit**

```bash
git add presentation/server/main.py tests/presentation/test_lifespan_consolidation.py
git commit -m "feat(c7-v3): discovery 启动补跑（offline 容错，幂等）

- _discovery_missed_last_run() 读 search_run 最新 started_at vs 昨日 02:00
- lifespan startup 错过则 _run_discovery_subprocess 异步补跑（DETACHED 不阻塞 uvicorn）
- 幂等：discovery run_daemon_cycle 早退（converged 跳过）+ 轮次/seed 派生去重
- 4 用例：无记录/陈旧/近期/补跑触发

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4 (V4)：schtasks ONSTART + 删 start_all.py

**Files:**
- Modify: `ops/manage_ops_schtasks.py`（加 `--register-server` / `--unregister-discovery`）
- Create: `scripts/start_server.bat`
- Delete: `ops/start_all.py` + `scripts/start_all.bat`
- Test: `tests/test_manage_ops_schtasks.py`（加新命令测试）

**Interfaces:**
- Consumes: Windows schtasks（/SC ONSTART /TR /RU）；`discovery.schtasks.unregister`（既有）。
- Produces: `manage_ops_schtasks.register_server()` / `unregister_discovery()`；`scripts/start_server.bat`。

- [ ] **Step 1：写失败测试（tests/test_manage_ops_schtasks.py，如不存在则新建）**

```python
# -*- coding: utf-8 -*-
"""C-7 V4：manage_ops_schtasks 加 --register-server / --unregister-discovery。"""
from unittest.mock import patch
from ops.manage_ops_schtasks import register_server, unregister_discovery, main


def test_register_server_creates_onstart_task():
    """register_server：schtasks /SC ONSTART /TR start_server.bat /RU <user>。"""
    cmds = []
    with patch("ops.manage_ops_schtasks._schtasks", side_effect=lambda a: cmds.append(a) or 0):
        register_server(user="TestUser")
    # 找到 /Create 命令
    create_cmd = [c for c in cmds if "/Create" in c]
    assert len(create_cmd) == 1
    cmd = create_cmd[0]
    assert "/SC" in cmd and "ONSTART" in cmd
    assert "/TN" in cmd and "QuanterServer" in cmd
    assert "/TR" in cmd and "start_server.bat" in cmd
    assert "/RU" in cmd and "TestUser" in cmd


def test_unregister_discovery_deletes_task():
    """unregister_discovery：schtasks /Delete QuanterDiscoveryDaemon /F。"""
    cmds = []
    with patch("ops.manage_ops_schtasks._schtasks", side_effect=lambda a: cmds.append(a) or 0):
        unregister_discovery()
    delete_cmds = [c for c in cmds if "/Delete" in c]
    assert any("QuanterDiscoveryDaemon" in c for c in delete_cmds)


def test_main_register_server_flag():
    """main(--register-server) 调 register_server。"""
    with patch("ops.manage_ops_schtasks.register_server") as rs:
        main(["--register-server", "--user", "TestUser"])
    rs.assert_called_once()
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/test_manage_ops_schtasks.py -v`
Expected: FAIL（`register_server` / `unregister_discovery` 未定义）。

- [ ] **Step 3：实现——manage_ops_schtasks 加新命令**

Edit `ops/manage_ops_schtasks.py`：

**3a. 加 register_server / unregister_discovery 函数**（在 `unregister_pipeline_brief` 后）：

```python
def register_server(user: str | None = None) -> None:
    """C-7 V4：注册 QuanterServer schtasks ONSTART（session 0 后台起 python -m trading）。

    物理意图（spec §3.4）：替代 start_all.py 的 subprocess.Popen DETACHED + 「启动」文件夹
    ONLOGON。ONSTART 开机即跑（session 0，不依赖用户登录/logoff），适配生产机不 7x24。
    /RU：运行账户（user 参数；缺省当前用户）。/RP 密码由调用方交互输入（不进代码）。
    """
    user = user or os.environ.get("USERNAME", "")
    rc = _schtasks(["/Create", "/SC", "ONSTART", "/TN", "QuanterServer",
                    "/TR", str(ROOT / "scripts" / "start_server.bat"),
                    "/RU", user, "/F"])
    print(f"{'OK' if rc == 0 else 'FAIL(需 /RP 密码?)'} QuanterServer @ ONSTART → start_server.bat (user={user})")
    print("⚠️ 若上 FAIL：schtasks ONSTART 需用户密码，手动跑 "
          "`schtasks /Create /SC ONSTART /TN QuanterServer /TR <bat> /RU <user> /RP <密码> /F`")


def unregister_discovery() -> None:
    """C-7 V4：退 discovery QuanterDiscoveryDaemon schtasks（收编 lifespan APScheduler 后防双触发）。"""
    rc = _schtasks(["/Delete", "/TN", "QuanterDiscoveryDaemon", "/F"])
    print(f"{'deleted' if rc == 0 else 'skip(not exists)'} QuanterDiscoveryDaemon")
```

**3b. main() argparse 加新参数**：

```python
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="观测层 schtasks 管理（C-7：加 register-server / unregister-discovery）"
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--register-server", action="store_true",
                   help="C-7：注册 QuanterServer ONSTART（python -m trading 后台）")
    g.add_argument("--unregister", action="store_true")
    g.add_argument("--unregister-pipeline-brief", action="store_true")
    g.add_argument("--unregister-discovery", action="store_true",
                   help="C-7：退 QuanterDiscoveryDaemon（收编 lifespan）")
    g.add_argument("--rerun", metavar="TASK")
    p.add_argument("--user", default=None, help="schtasks /RU 用户（register-server 用）")
    args = p.parse_args(argv)
    if args.register:
        register()
    elif args.register_server:
        register_server(user=args.user)
    elif args.unregister:
        unregister()
    elif args.unregister_pipeline_brief:
        unregister_pipeline_brief()
    elif args.unregister_discovery:
        unregister_discovery()
    elif args.list:
        list_tasks()
    elif args.rerun:
        rerun(args.rerun)
    return 0
```

**3c. `import os`**（manage_ops_schtasks 顶部，若未 import）。

- [ ] **Step 4：新建 scripts/start_server.bat**

```bat
@echo off
chcp 65001 >nul
cd /d "F:\quanter"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv310\Scripts\python.exe" -m trading
```

- [ ] **Step 5：删 ops/start_all.py + scripts/start_all.bat**

Run: `git rm ops/start_all.py scripts/start_all.bat`

**注**：删前确认无其它引用（grep `start_all` 在 docs/scripts/ops，确认仅历史 doc 提及，无运行时依赖——start_all 职责已 lifespan 收编 + schtasks ONSTART）。

- [ ] **Step 6：跑测试验证通过**

Run: `pytest tests/test_manage_ops_schtasks.py -v`
Expected: 3 用例 PASS。

- [ ] **Step 7：commit**

```bash
git add ops/manage_ops_schtasks.py scripts/start_server.bat tests/test_manage_ops_schtasks.py
git rm ops/start_all.py scripts/start_all.bat
git commit -m "feat(c7-v4): schtasks ONSTART + 删 start_all.py（真单进程启动链）

- manage_ops_schtasks 加 register_server（QuanterServer ONSTART / python -m trading）
- 加 unregister_discovery（退 QuanterDiscoveryDaemon，收编 lifespan 防 # 双触发）
- 新建 scripts/start_server.bat（cd + python -m trading，schtasks session 0 后台包裹）
- 删 ops/start_all.py + scripts/start_all.bat（职责全收编 lifespan + schtasks ONSTART）
- /RU 用户（/RP 密码手动输，不进代码）；ONSTART 适配生产机不 7x24

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5 (V5)：全量回归 + 验收

**Files:** 无代码改动（验证性 Task）。

- [ ] **Step 1：grep 确认 start_all 删 + start_server 新建**

Run: `ls F:/quanter/ops/start_all.py F:/quanter/scripts/start_all.bat 2>&1 | grep -c "No such" || echo "应 2（都删）"`
Run: `ls F:/quanter/scripts/start_server.bat`
Expected: start_all.py + start_all.bat 不存在；start_server.bat 存在。

- [ ] **Step 2：全量回归（spec §6 验收 6 · C-6 后 1164 基线零退化）**

Run: `cd F:/quanter && .venv310/Scripts/python.exe -m pytest tests/ -q`
Expected: **1164+ passed / 0 failed**（C-6 基线 1164 + V1-V4 新增测试 ~12 → 约 1176 passed）。**0 failed 硬指标**。

若 failed：按失败信息回 V1-V4 修复。

- [ ] **Step 3：spec §6 验收 1-7 逐条核对**

| # | 验收项 | 核对 |
|---|---|---|
| 1 | connect 进 lifespan（5 BOTS start/stop 软降级） | V1 测试 PASS |
| 2 | discovery 进 lifespan APScheduler cron 02:00 | V2 测试 PASS（engine.sched add_job） |
| 3 | discovery 启动补跑（offline 跨 02:00 异步补跑幂等） | V3 测试 PASS |
| 4 | start_all.py 删 + start_server.bat + schtasks QuanterServer ONSTART | V4 Step 1 grep + 测试 PASS |
| 5 | 历史 schtasks 退（discovery + pipeline/brief） | V4 unregister_discovery + 既有 unregister_pipeline_brief |
| 6 | 全量回归零退化 | Step 2 `0 failed` |
| 7 | C-4/C-5/C-6 决议不变 | grep gate/clock 未误改 |

- [ ] **Step 4：手动 smoke 清单（待用户执行，沙箱不可起 schtasks/server）**

记录到 report（不实际跑）：
1. `python -m manage_ops_schtasks --register-server --user <用户>` 注册 QuanterServer ONSTART（若需 /RP 密码手动）。
2. `python -m manage_ops_schtasks --unregister-discovery` 退 QuanterDiscoveryDaemon。
3. 重启机器 → QuanterServer ONSTART 触发 → uvicorn :8000 起（session 0 后台）→ lifespan 装 connect（5 bots）+ discovery cron 02:00 + 补跑检查。
4. 验证：钉钉 5 connect 机器人在线 + discovery 02:00 跑（或补跑）+ engine 四 cron（pre_open/stop_loss/post_close/pipeline_then_eod）。

- [ ] **Step 5：commit（如有 doc/验收记录更新）**

```bash
git add <改动>
git commit -m "test(c7-v5): 全量回归 + spec §6 验收 1-7 全绿

- 全量 1176 passed / 0 failed（C-6 基线 1164 + V1-V4 新增 ~12）
- spec §6 验收 1-7 逐条绿
- 手动 smoke 清单（register-server / unregister-discovery / 重启验 lifespan 装 connect+discovery）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

- [ ] **Step 6：更新 memory（controller 执行）**

C-7 merge 后 controller 写 `~/.claude/projects/F--quanter/memory/c7-start-all-consolidation-status.md` + `MEMORY.md` 索引。

---

## Self-Review

**1. Spec 覆盖**：
- spec §3.1 connect 进 lifespan → Task 1 ✓
- spec §3.2 discovery cron 02:00 → Task 2 ✓（subprocess 替代 ProcessPoolExecutor，plan 决策：复用 cli 装配更简，spec §3.2 意图不变）
- spec §3.3 discovery 启动补跑 → Task 3 ✓
- spec §3.4 schtasks ONSTART 删 start_all → Task 4 ✓
- spec §6 验收 1-7 → Task 5 逐条 ✓

**2. Placeholder 扫描**：无 TBD/TODO；每 step 含完整代码或精确 grep；Task 1 Step 1 测试假设 `lifespan_startup_only` 抽出（implementer 按 main.py 实际 lifespan 结构调整测试范式——已注明）；Task 4 Step 5 删 start_all 前 grep（已注明）。

**3. 类型一致性**：`_run_discovery_subprocess()` / `_discovery_missed_last_run() -> bool` / `register_server(user)` / `unregister_discovery()` 签名贯穿 V2-V4 一致 ✓；`app.state.connect_bots` / `app.state.trading_engine`（既有）一致 ✓。

**4. 关键设计决策（plan 阶段定，spec 标「plan 定」的已落定）**：
- discovery 调度：engine.sched add_job（复用 TradingEngine.sched，避免多 scheduler 实例）+ subprocess `python -m discovery daemon`（复用 cli 装配，子进程隔离，替代 spec §3.2 的 ProcessPoolExecutor——更简且与 schtasks 当前语义等价）。
- discovery 上次完成时间：直接读 `search_run` 表最新 `started_at`（sqlite3 直查，不按 hash 过滤，避免 freeze 重型）。
- schtasks 凭证：`/RU <user>`（env USERNAME 缺省），`/RP` 密码手动输（不进代码，register_server 提示）。
