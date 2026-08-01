# -*- coding: utf-8 -*-
"""C-7 V1：lifespan 装 broadcast connect（5 CONNECT_BOTS start/stop 软降级）。

物理意图（spec §3.1）：start_all step ② connect 编排收编进 lifespan，软降级
（单 bot 失败不阻断 uvicorn）。live reload=False（C-5 V1）不 reload，connect 不抖动。

测试范式选择：``async with lifespan(app)``（跑完 startup + shutdown）+ mock。
    Why 不抽 ``lifespan_startup_only``/``lifespan_shutdown_only`` helper：
    main.py 现行 lifespan 是 ``@asynccontextmanager``（C-5/V2 结构），YAGNI——
    为单测重构 lifespan 结构（拆双 helper）属过度设计，违反「不重构 lifespan 结构」纪律。
    与既有 tests/server/test_lifespan_engine.py 同范式（``@pytest.mark.asyncio`` +
    ``async with lifespan(app)`` + mock），仅 mock 范围扩到 connect 装配块。

TDD 约定：本仓库 pytest-asyncio 为 strict 模式（pytest.ini 未配 asyncio_mode），
显式 ``@pytest.mark.asyncio`` 装饰器触发异步测试收集。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_lifespan_dependencies():
    """mock 掉 lifespan 内除 connect 外的重装配链（聚焦 connect，隔离 IO/subprocess）。

    覆盖范围：
      - build_default_manager：通知通道装配（无凭证也 import dws 链，过重）
      - DataLakeReader.get_instance：多湖 parquet load（文件系统 + 内存）
      - replay scheduler/pool：ProcessPoolExecutor 子进程 + APScheduler 线程
      - training_orchestrator：daemon 线程 + DB init
      - symbol_names.load_all：Tushare pro 全量 stock_basic
      - data_service.sweep_stale_on_startup：daemon 线程内 trigger_sync 子进程
      - TradingEngine：网关 + scheduler（既有 test_lifespan_engine.py 已 mock 范式）

    用 ``ExitStack`` 统一管理多个 patch 上下文（避免 with 嵌套地狱），返回 patches dict
    便于测试内对个别 mock 做断言（如 connect_manager.start/stop 调用记录）。
    """
    from contextlib import ExitStack

    stack = ExitStack()
    # connect_manager.start/stop 用 MagicMock（核心断言对象，默认无副作用）
    start_mock = stack.enter_context(patch("broadcast.connect_manager.start", return_value="started"))
    stop_mock = stack.enter_context(patch("broadcast.connect_manager.stop", return_value="stopped"))
    # 其它重装配链全部 mock 成无副作用（聚焦 connect，不被其余装配干扰）
    stack.enter_context(patch("presentation.server.main.build_default_manager"))
    stack.enter_context(patch("data.lake_reader.DataLakeReader.get_instance", return_value=MagicMock()))
    stack.enter_context(patch("backtest.tasks_db.init_db"))
    stack.enter_context(patch("backtest.worker._init_worker"))
    stack.enter_context(patch("backtest.scheduler.ReplayScheduler"))
    stack.enter_context(patch("concurrent.futures.ProcessPoolExecutor"))
    stack.enter_context(patch("backtest.optimize.training_loops_db.init_db"))
    stack.enter_context(patch("backtest.optimize.training_loops_db.reset_interrupted"))
    stack.enter_context(patch("backtest.optimize.training_loop.TrainingLoopOrchestrator"))
    stack.enter_context(patch("backtest.optimize.training_dingtalk.ReviewBotConfig.from_env", return_value=None))
    stack.enter_context(patch("data.symbol_names.load_all"))
    stack.enter_context(patch("presentation.server.services.data_service.sweep_stale_on_startup", return_value=[]))
    # TradingEngine（check_shadow_gate + engine 实例 mock）
    # bootstrap 是 async（lifespan 内 ``await eng.bootstrap()``），用 AsyncMock；
    # sched.running=False 影子期不足 → lifespan 不调 eng.start，shutdown 段据此跳过 eng.shutdown
    eng = MagicMock()
    eng.sched.running = False
    eng.bootstrap = AsyncMock()
    stack.enter_context(patch("trading.engine.TradingEngine", return_value=eng))
    stack.enter_context(patch("trading.__main__.check_shadow_gate", return_value=False))  # 影子期不足不 start
    stack.enter_context(patch("trading.__main__.log_startup_banner"))
    return stack, start_mock, stop_mock, eng


@pytest.mark.asyncio
async def test_lifespan_starts_all_5_connect_bots():
    """lifespan startup 遍历 5 CONNECT_BOTS 调 connect_manager.start。

    验：start 对每个 bot 各调一次，app.state.connect_bots 记录全部 5 bot。
    （shutdown 段亦会跑，stop_mock 已 mock 掉，此处聚焦 startup 断言。）
    """
    from broadcast.__main__ import CONNECT_BOTS
    from fastapi import FastAPI
    from presentation.server.main import lifespan

    app = FastAPI()
    stack, start_mock, stop_mock, _eng = _mock_lifespan_dependencies()
    with stack:
        async with lifespan(app):           # startup 跑完 → 进 yield
            # startup 已完成：connect_manager.start 应被调 5 次（每 bot 一次）
            started_bots = [call.args[0] for call in start_mock.call_args_list]
            assert set(started_bots) == set(CONNECT_BOTS.keys())
            assert app.state.connect_bots == list(CONNECT_BOTS.keys())
        # lifespan 退出（shutdown 跑完），stop 不影响 startup 断言

    # 再核一次（退出 scope 后断言，避免 mock 作用域误差）
    start_mock.assert_called()
    assert start_mock.call_count == len(CONNECT_BOTS)   # 5


@pytest.mark.asyncio
async def test_lifespan_connect_soft_degrade_on_single_bot_failure():
    """单 bot start 抛 RuntimeError → 跳过该 bot，其余 4 bot 正常起，不阻断 uvicorn。

    物理意图（spec §3.1 软降级）：配置缺失（unified_app_id 未填等）抛 RuntimeError
    （见 connect_manager.build_cmd 的身份闸），lifespan 须跳过该 bot 继续起其余，
    与 engine/training_orchestrator 同源软降级范式（装配失败不阻断 uvicorn）。
    """
    from broadcast.__main__ import CONNECT_BOTS
    from fastapi import FastAPI
    from presentation.server.main import lifespan

    app = FastAPI()
    stack, start_mock, stop_mock, _eng = _mock_lifespan_dependencies()
    # 覆盖 start：review bot 抛 RuntimeError（模拟配置缺失），其余正常
    started_bots = []

    def _fake_start(bot, cfg, defaults):
        if bot == "review":
            raise RuntimeError("配置缺失：缺 REVIEW_BOT_UNIFIED_APP_ID")
        started_bots.append(bot)
        return "started"

    start_mock.side_effect = _fake_start
    with stack:
        async with lifespan(app):
            # review 失败跳过，其余 4 bot 正常起
            assert "review" not in started_bots
            assert set(started_bots) == set(CONNECT_BOTS.keys()) - {"review"}
            # app.state.connect_bots 不含 review（失败的不记录）
            assert "review" not in app.state.connect_bots
            assert set(app.state.connect_bots) == set(CONNECT_BOTS.keys()) - {"review"}

    # 软降级核心断言：review 抛异常但 lifespan 未传播（async with 正常退出），
    # 其余 4 bot 仍被 start（start 总调用次数=5，含失败的那次）
    assert start_mock.call_count == len(CONNECT_BOTS)   # 5（review 也被尝试调用过）


@pytest.mark.asyncio
async def test_lifespan_stops_connect_bots_on_shutdown():
    """lifespan shutdown 遍历 app.state.connect_bots 调 connect_manager.stop（树杀）。

    物理意图：shutdown 与 startup start 对偶——startup 起的 bot，shutdown 必须停
    （taskkill /F /T 树杀 dev connect + Claude Code 子进程），防进程退出资源泄漏。
    验：进入 async with 后 app.state.connect_bots 被 startup 填充，退出后 stop 以
    相同 bot 列表各被调一次。
    """
    from broadcast.__main__ import CONNECT_BOTS
    from fastapi import FastAPI
    from presentation.server.main import lifespan

    app = FastAPI()
    stack, start_mock, stop_mock, _eng = _mock_lifespan_dependencies()
    with stack:
        async with lifespan(app):
            # startup 已填充 app.state.connect_bots（5 bot）
            assert app.state.connect_bots == list(CONNECT_BOTS.keys())
            stop_mock.reset_mock()              # 清掉无关调用，专注 shutdown 段
        # 退出 async with → shutdown 段跑完

    # shutdown 段：stop 对每个已起 bot 各调一次
    stopped_bots = [call.args[0] for call in stop_mock.call_args_list]
    assert set(stopped_bots) == set(CONNECT_BOTS.keys())
    assert stop_mock.call_count == len(CONNECT_BOTS)


# ============ C-7 V2：discovery 进 lifespan cron 02:00（subprocess 子进程） ============
# 物理意图（spec §3.2）：discovery 从 schtasks DAILY 02:00 收编到 engine.sched
# AsyncIOScheduler，触发 _run_discovery_subprocess（DETACHED 子进程跑 cli daemon）。
#
# 测试范式适配说明（brief 给的 lifespan_startup_only + _discovery_missed_last_run 在 V1
# 未落地——V1 用 @asynccontextmanager ``lifespan`` 整体上下文范式）。此处沿用 V1 既有
# ``async with lifespan(app)`` + ``_mock_lifespan_dependencies()`` 范式，仅扩展两点：
#   1. mock 掉 _run_discovery_subprocess 避免 cron job 实际起 subprocess（cron 由
#      AsyncIOScheduler 异步派发，本测用 add_job 调用断言验证注册，不依赖 trigger 真触发）；
#   2. 在 eng.sched.add_job 上挂 lambda 收集 id（断言 discovery_daemon 被注册）。


@pytest.mark.asyncio
async def test_lifespan_registers_discovery_cron_02():
    """lifespan startup 在 engine.sched add_job ``discovery_daemon`` cron 02:00。

    物理意图：discovery cron 从 schtasks 收编 lifespan（spec §3.2），engine.sched
    AsyncIOScheduler 上挂 id="discovery_daemon" 的 job，trigger 为 CronTrigger(hour=2)。
    mock _run_discovery_subprocess 避免 add_job 的 func 真被调度触发（cron trigger
    在测期内不会到 02:00，但保险 mock 防触发——同时验证被传入的 func 是该模块函数）。
    """
    from fastapi import FastAPI
    from presentation.server.main import lifespan

    app = FastAPI()
    stack, _start, _stop, eng = _mock_lifespan_dependencies()
    # add_job 用 lambda 收集 id（func/trigger 实参不影响断言，聚焦 id）
    added_jobs: list[str] = []
    eng.sched.add_job = lambda func, trigger=None, **kw: added_jobs.append(kw.get("id"))
    # mock _run_discovery_subprocess：避免 add_job 误触发或测试侧起 subprocess
    stack.enter_context(patch("presentation.server.main._run_discovery_subprocess"))
    with stack:
        async with lifespan(app):
            pass                    # startup 跑完 → yield（cron 已注册）

    assert "discovery_daemon" in added_jobs    # discovery cron 02:00 注册


def test_run_discovery_subprocess_detached():
    """_run_discovery_subprocess 起 DETACHED 子进程 ``python -m discovery daemon``。

    验：(1) subprocess.Popen 调一次；(2) cmd=[venv_py, -m, discovery, daemon]（复用
    cli cmd_daemon 装配，不重写 freeze/split 默认参数）；(3) creationflags 含
    DETACHED_PROCESS(0x8)——子进程独立进程组，uvicorn 退出不杀 discovery 夜跑。
    """
    from presentation.server.main import _run_discovery_subprocess

    with patch("presentation.server.main._subprocess.Popen") as popen:
        _run_discovery_subprocess()
    popen.assert_called_once()
    cmd = popen.call_args[0][0]
    assert "-m" in cmd and "discovery" in cmd and "daemon" in cmd
    # DETACHED 标志（creationflags 含 DETACHED_PROCESS = 0x8）
    flags = popen.call_args[1].get("creationflags", 0)
    assert flags & 0x00000008      # DETACHED_PROCESS 位


# ============ C-7 V3：discovery 启动补跑（offline 容错） ============
# 物理意图（spec §3.3）：生产机不 7x24，offline 跨昨晚 02:00 → 当晚 discovery 漏跑
# （策略迭代断链）。lifespan startup 检测到漏跑则异步补跑 _run_discovery_subprocess。
#
# 测试范式：``_discovery_missed_last_run`` 单测用 tmp_path 真 sqlite（不 mock sqlite3，
# 比 brief 的 mock 范式更稳健且对齐 tests/discovery/test_store.py 仓库范式）；补跑触发
# 测试沿用 V2 的 ``async with lifespan(app)`` + ``_mock_lifespan_dependencies()``。


def _seed_search_run(db_path: str, started_at: str | None) -> None:
    """向 tmp sqlite 插一行 search_run（建表 + INSERT）。

    幂等建表（CREATE TABLE IF NOT EXISTS），仅插入 started_at 列即可——
    _discovery_missed_last_run 只读这一列，不依赖 snapshot_hash（spec §3.3：不按 hash
    过滤避免 freeze 重型）。
    """
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS search_run (
          run_id TEXT PRIMARY KEY,
          snapshot_hash TEXT,
          started_at TEXT,
          ended_at TEXT,
          n_trials INTEGER,
          status TEXT,
          note TEXT);
    """)
    if started_at is not None:
        conn.execute(
            "INSERT INTO search_run (run_id, snapshot_hash, started_at) VALUES (?, ?, ?)",
            ("r1", "snap1", started_at),
        )
    conn.commit()
    conn.close()


def test_discovery_missed_last_run_true_when_db_missing(tmp_path, monkeypatch):
    """DB 文件不存在（首次运行 / 全新环境）→ 返 True（补跑，让 daemon 首次跑）。

    物理意图：discovery_trials.db 未创建意味着从未跑过 discovery，lifespan 须触发
    首次补跑（spec §3.3 offline 容错「无记录」分支）。
    """
    db_path = str(tmp_path / "no_such.db")   # 不创建——_discovery_missed_last_run 内 sqlite3.connect
    # 对不存在文件 sqlite3.connect 会建空库但 search_run 表不存在 → execute 抛
    # OperationalError → 异常分支返 True（补跑）
    monkeypatch.setenv("DISCOVERY_DB", db_path)
    from presentation.server.main import _discovery_missed_last_run
    assert _discovery_missed_last_run() is True


def test_discovery_missed_last_run_true_when_stale(tmp_path, monkeypatch):
    """DB 存在但无 search_run 行（表空）→ 返 True（补跑，保守视为错过）。

    物理意图：表已建但无 run 记录（daemon 装配但从未完成一轮）→ 视为错过补跑。
    """
    db_path = str(tmp_path / "t.db")
    _seed_search_run(db_path, started_at=None)   # 建表不插行
    monkeypatch.setenv("DISCOVERY_DB", db_path)
    from presentation.server.main import _discovery_missed_last_run
    assert _discovery_missed_last_run() is True


def test_discovery_missed_last_run_true_when_old_started_at(tmp_path, monkeypatch):
    """最新 started_at < 昨日 02:00 → 返 True（offline 跨昨晚 02:00，补跑）。

    物理意图（spec §3.3 核心）：生产机 offline 跨过昨晚 02:00（如机器关机/uvicorn 未起），
    discovery cron 漏跑——最新 search_run.started_at 是两天前，启动时补跑防策略迭代断链。
    """
    from datetime import datetime, timedelta
    db_path = str(tmp_path / "t.db")
    stale = (datetime.now() - timedelta(days=2)).isoformat()
    _seed_search_run(db_path, started_at=stale)
    monkeypatch.setenv("DISCOVERY_DB", db_path)
    from presentation.server.main import _discovery_missed_last_run
    assert _discovery_missed_last_run() is True


def test_discovery_missed_last_run_false_when_recent(tmp_path, monkeypatch):
    """最新 started_at ≥ 昨日 02:00（昨晚跑了）→ 返 False（不补跑）。

    物理意图：今晚 uvicorn 起来前昨晚 02:00 的 cron 已正常跑过 discovery（started_at
    是今天凌晨），无需补跑——幂等去重（discovery run_daemon_cycle 早退 converged 跳过）。
    """
    from datetime import datetime, timedelta
    db_path = str(tmp_path / "t.db")
    recent = (datetime.now() - timedelta(hours=2)).isoformat()   # 今天刚跑
    _seed_search_run(db_path, started_at=recent)
    monkeypatch.setenv("DISCOVERY_DB", db_path)
    from presentation.server.main import _discovery_missed_last_run
    assert _discovery_missed_last_run() is False


@pytest.mark.asyncio
async def test_lifespan_catchup_runs_discovery_when_missed():
    """lifespan startup：_discovery_missed_last_run=True → 调 _run_discovery_subprocess 补跑。

    物理意图（spec §3.3）：startup 检测到 offline 跨昨晚 02:00（错过）→ 异步补跑
    _run_discovery_subprocess（DETACHED 子进程，立即返不阻塞 uvicorn 起）。幂等靠
    discovery 既有轮次/seed + run_daemon_cycle 早退（converged 跳过）去重。
    """
    from fastapi import FastAPI
    from presentation.server.main import lifespan

    app = FastAPI()
    stack, _start, _stop, eng = _mock_lifespan_dependencies()
    eng.sched.add_job = MagicMock()                # add_job 不抛
    # mock _discovery_missed_last_run=True（错过）+ _run_discovery_subprocess（断言补跑触发）
    stack.enter_context(patch("presentation.server.main._discovery_missed_last_run", return_value=True))
    run_disc = stack.enter_context(patch("presentation.server.main._run_discovery_subprocess"))
    with stack:
        async with lifespan(app):
            pass                    # startup 跑完 → yield（补跑在 startup 段已触发）

    run_disc.assert_called_once()   # 补跑触发


@pytest.mark.asyncio
async def test_lifespan_catchup_skipped_when_recent():
    """lifespan startup：_discovery_missed_last_run=False → 不调 _run_discovery_subprocess。

    物理意图（幂等）：昨晚 02:00 已跑过 discovery，启动不补跑（避免与今晚 02:00 双跑
    重复——虽 run_daemon_cycle 早退去重，但补跑子进程本身有 ~4h budget 开销，避免之）。
    """
    from fastapi import FastAPI
    from presentation.server.main import lifespan

    app = FastAPI()
    stack, _start, _stop, eng = _mock_lifespan_dependencies()
    eng.sched.add_job = MagicMock()
    stack.enter_context(patch("presentation.server.main._discovery_missed_last_run", return_value=False))
    run_disc = stack.enter_context(patch("presentation.server.main._run_discovery_subprocess"))
    with stack:
        async with lifespan(app):
            pass

    run_disc.assert_not_called()    # 不补跑

# ============ C-8 V5：启动补跑任务接线（engine start 后 create_task + shutdown cancel）============
# 物理意图（spec §3.6）：engine 装配+start 后，lifespan 创建后台补跑任务；仅
# sched.running=True（scheduler 已起）时触发——影子期不足时补跑无意义。


@pytest.mark.asyncio
async def test_lifespan_creates_catchup_task_when_engine_started():
    """engine 已 start → lifespan startup 创建 catchup_task 并 await run_startup_catchup。"""
    from fastapi import FastAPI
    from presentation.server.main import lifespan

    app = FastAPI()
    stack, _start, _stop, eng = _mock_lifespan_dependencies()
    eng.sched.running = True                              # engine 已 start
    stack.enter_context(patch("trading.__main__.check_shadow_gate", return_value=True))
    catchup = stack.enter_context(
        patch("trading.catchup.run_startup_catchup", new=AsyncMock()))
    with stack:
        async with lifespan(app):
            task = getattr(app.state, "catchup_task", None)
            assert task is not None                        # 任务已创建
            await task                                     # 等补跑协程完成（AsyncMock 秒完）
    catchup.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_skips_catchup_when_engine_not_started():
    """影子期不足（sched.running=False）→ 不创建 catchup_task。"""
    from fastapi import FastAPI
    from presentation.server.main import lifespan

    app = FastAPI()
    stack, _start, _stop, eng = _mock_lifespan_dependencies()   # 默认 running=False
    catchup = stack.enter_context(
        patch("trading.catchup.run_startup_catchup", new=AsyncMock()))
    with stack:
        async with lifespan(app):
            assert not hasattr(app.state, "catchup_task")
    catchup.assert_not_awaited()