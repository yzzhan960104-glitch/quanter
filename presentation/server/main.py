# -*- coding: utf-8 -*-
"""
FastAPI 应用入口

职责：
1. 创建 FastAPI 应用实例
2. 注册 CORS 中间件（允许前端 Vite dev server 跨域访问）
3. 挂载 API 路由（/api/v1/logs, /api/v1/trading 等）
4. 提供健康检查端点

启动方式：
    uvicorn presentation.server.main:app --reload --host 0.0.0.0 --port 8000

设计原则：
- 应用入口仅做组装，不包含业务逻辑
- CORS 配置从 http/config.py 读取，不硬编码
- 路由版本化 /api/v1/，预留后续版本空间
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from presentation.server.http.config import CORS_ORIGINS, LOG_CONFIG
from presentation.server.http._responses import StrictJSONResponse
# API 鉴权依赖（B-1）：挂在敏感 router（trading/caisen/data/review）上，
# token 未配置=开发态放行（WARNING），生产须配 QUANTER_API_TOKEN。
from presentation.server.http.auth import require_write
from presentation.server.api.v1.logs import (
    RingBufferLogHandler,
    log_stream_hub,
    router as logs_router,
)
# B2-3：进程拓扑观测端点（三合一 + 客户端 + 队列 + 网关态一屏）
from presentation.server.api.v1.ops import router as ops_router
# 宏观/板块/因子只读端点（T16）：读内存湖 + CreditRegime，零写入，
# 供给前端驾驶舱（T17 /dashboard）宏观灯/信贷曲线/板块流/ATR 四视图。
from presentation.server.api.v1.macro import router as macro_router
# 实盘交易（优雅降级真接 QMT；无 xtquant/缺凭证时 /status 返 unavailable，不阻断 lifespan）
from presentation.server.api.v1.trading import router as trading_router
# AI 参数训练 loop 路由（Spec 3 Task 6）：start/get/list/submit_review 四端点，
# 驱动 orchestrator 状态机（CREATED→RUNNING→ANALYZING→AWAITING_REVIEW→…→DONE）。
# 钉钉审核进程内调 handler 不走 HTTP；此 router 仅对外暴露状态查询 + 启停 + 审核提交。
from presentation.server.api.v1.training import router as training_router
# 数据湖资产路由（层级一）：扫描 parquet mtime + 哨兵推导状态，触发同步起 daemon 子进程
from presentation.server.api.v1.data import router as data_router
# AI 复盘路由（层级六）：GLM 调用 + 三级降级，CPU/网络阻塞走线程池
from presentation.server.api.v1.review import router as review_router
# Phase C 研究提案路由（2026-08-03）：Agent 提案生成/验证/钉钉审批/发布桥。
from presentation.server.api.v1.research import router as research_router
# 通知装配：Telegram/企微/钉钉三通道按凭证装配，缺凭证跳过对应通道
from infra.notifier import build_default_manager

# C-7 V2：discovery daemon cron 调度（subprocess 子进程隔离，复用 cli daemon 装配）。
# 物理意图（spec §3.2）：discovery 从 schtasks DAILY 02:00 收编到 lifespan——
# engine.sched AsyncIOScheduler cron 02:00 触发本模块级函数，DETACHED subprocess 起
# `python -m discovery daemon`。模块级（非闭包）：(1) 测试可直接 mock；
# (2) 与 broadcast/connect_manager.py 等既有「模块级 subprocess 入口」范式一致
# （C-7 后 ops/start_all.py 已删，本模块为该范式现存源头之一）；(3) V3 启动补跑
# （startup 内同步调一次）复用同函数，避免逻辑重复。
import subprocess as _subprocess

# APScheduler 3.x 工作日语义：0=周一（非标准 cron 0=周日）。digest 推送与 engine
# 的 pipeline/pre_open/post_close 必须用 ``mon-fri``（"1-5" 实为周二~周六，
# 2026-08-03 周一断链实证，tests/test_workday_cron.py 钉死）。
_DIGEST_CRON_DEFAULT = "30 18 * * mon-fri"

# Windows DETACHED 标志（broadcast/connect_manager.py 等既有范式，C-7 前 ops/start_all.py 同源）：
#   CREATE_NEW_PROCESS_GROUP(0x200) → 子进程独立进程组（Ctrl+C 不传播）；
#   DETACHED_PROCESS(0x8)           → 无控制台（独立于父进程 uvicorn 的 stdio）。
# 二者组合：uvicorn 退出/重启不杀 discovery 夜跑子进程（长任务不依附 server 生命周期）。
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008


def _discovery_low_power_allowed(now=None) -> bool:
    """24h 低功率模式的窗口判定：避开盘中与数据链时段（2026-08-03）。

    物理意图：低功率 = 全天小批慢跑，但不能抢交易时段资源（9:00-16:59 盘中/
    盘后对账）与 18:00 pipeline+18:30 digest 窗口。允许小时：0-8 与 17、19-23；
    9-16、18 跳过。纯函数（now 可注入，默认本地时间）。
    """
    from datetime import datetime
    now = now or datetime.now()
    return now.hour in (0, 1, 2, 3, 4, 5, 6, 7, 8, 17, 19, 20, 21, 22, 23)


def _run_discovery_subprocess(low_power: bool | None = None) -> None:
    """discovery daemon cron job：subprocess 起 ``python -m discovery daemon``（子进程隔离）。

    物理意图（spec §3.2）：discovery 收编 lifespan 后，触发点由 schtasks 改为
    engine.sched cron 02:00（本函数）。用 subprocess 而非 ProcessPoolExecutor：
      (1) 复用 cli ``cmd_daemon`` 全套装配（build_default_manager 钉钉通道 + freeze
          /holdout_split/run_daemon 默认参数），不重写装配逻辑（YAGNI）；
      (2) 子进程隔离——discovery 是重型全市场扫描（~4h budget），独立进程不阻塞
          uvicorn 事件循环，与既有 schtasks→run_daemon.bat 行为等价（行为不变）；
      (3) 与 schtasks 当前调 run_daemon.bat 语义对齐（.venv310/Scripts/python -m
          discovery daemon），lifespan 收编仅改触发点不改 daemon 调用契约。
    DETACHED：独立进程组，uvicorn 退出不杀 discovery 子进程（夜跑长任务不依附 server）。
    log 重定向 logs/discovery_cron.log（append），cron 触发的 stdout/stderr 落盘可溯源。
    """
    import os as _os
    from datetime import datetime
    from pathlib import Path
    # 24h 低功率模式（2026-08-03）：DISCOVERY_SCHEDULE=low-power 时每小时触发，
    # 每轮小批（1 组/单进程/K=24）。窗口内跳过（不触发子进程，零资源占用）。
    if low_power is None:
        low_power = _os.environ.get("DISCOVERY_SCHEDULE", "").lower() == "low-power"
    if low_power and not _discovery_low_power_allowed(datetime.now()):
        logging.getLogger(__name__).debug("discovery 低功率窗口跳过（盘中/数据链时段）")
        return
    _root = Path(__file__).resolve().parents[2]
    _venv_py = _root / ".venv310" / "Scripts" / "python.exe"
    _log_dir = _root / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_fh = (_log_dir / "discovery_cron.log").open("a", encoding="utf-8")
    _cmd = [str(_venv_py), "-m", "discovery", "daemon"]
    if low_power:
        # 低功率参数：每轮 1 组、单进程、K=24（24 次≈1 天不扩张才收敛）
        # --no-eval-replay-top：冠军 replay 复评全市场两段会拖长一倍，低功率每轮省掉
        # --tpe-trials 0：默认 10 个 TPE 会拖到 ~80min/轮，低功率每轮只跑 1 组 Sobol
        _cmd += ["--budget-groups", "1", "--n-proc", "1", "--k-rounds", "24",
                 "--no-eval-replay-top", "--tpe-trials", "0"]
    _subprocess.Popen(
        _cmd,
        cwd=str(_root),
        stdout=_log_fh,
        stderr=_subprocess.STDOUT,
        stdin=_subprocess.DEVNULL,
        creationflags=_CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS,
        close_fds=True,
    )


def _run_research_digest_push() -> None:
    """research digest 周期推送 cron job：DETACHED 子进程跑 ``research.digest --push``。

    物理意图（2026-08-03 · Agent 观察环推送腿）：每日盘后把研究摘要（实盘成交 +
    state_store 已实现盈亏 + 回测期望 + 漂移状态）推钉钉。用子进程而非进程内直调：
      (1) digest 读 CSV/SQLite + 钉钉 webhook 网络 IO，子进程隔离不阻塞 uvicorn 事件循环；
      (2) 与 discovery cron 同范式（DETACHED 独立进程组，server 重启不杀推送）；
      (3) 日志重定向 logs/research_digest.log（append），推送失败可溯源。
    """
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    _venv_py = _root / ".venv310" / "Scripts" / "python.exe"
    _log_dir = _root / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_fh = (_log_dir / "research_digest.log").open("a", encoding="utf-8")
    _subprocess.Popen(
        [str(_venv_py), "-m", "research.digest", "--push", "--proposals"],
        cwd=str(_root),
        stdout=_log_fh,
        stderr=_subprocess.STDOUT,
        stdin=_subprocess.DEVNULL,
        creationflags=_CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS,
        close_fds=True,
    )


def _discovery_missed_last_run() -> bool:
    """检查 discovery 是否错过昨晚 02:00（offline 容错补跑判定）。

    物理意图（spec §3.3）：生产机不 7x24，offline 跨 02:00 则当晚 discovery 漏跑
    （策略迭代断链）。本函数读 search_run 表最新 started_at（**不按 snapshot_hash 过滤，
    避免 freeze 重型**——freeze 涉及全市场扫描 + 哈希重建，启动补跑判定不能扛重活），
    与昨日 02:00 比——错过则 lifespan startup 触发补跑。

    幂等：discovery run_daemon_cycle 早退（status==converged 跳过）+ 轮次/seed 派生
    （[[discovery-engine-status]]），补跑 + 当晚 02:00 双跑靠此去重——即使补跑触发，
    daemon 内部对已收敛的 snapshot 直接跳过，不会重跑 trial。

    Why sqlite3 直查而非 discovery.store.connect：补跑判定是纯读（单条 SELECT），
    无需 WAL/Row 工厂/单点写锁装配；直查 sqlite3 更轻量，且表不存在（首次运行）时
    sqlite3.OperationalError 走异常分支返 True（补跑），语义对齐「无记录即补跑」。

    Returns:
      True = 错过（DB 不存在 / 表未建 / 无记录 / 时间解析失败 / 最新 started_at <
              昨日 02:00）→ 补跑；
      False = 昨晚跑了（最新 started_at ≥ 昨日 02:00）→ 不补跑。
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
        # DB 不存在 / 表未建（首次运行）→ 视为错过（补跑，让 daemon 首次跑）。
        # sqlite3.connect 对不存在文件会建空库但 search_run 表缺失 → execute 抛
        # OperationalError，落入此分支返 True（保守补跑）。
        return True
    if row is None:
        return True  # 表存在但无记录（daemon 装配但从未完成一轮）→ 补跑
    last_started = row["started_at"]
    try:
        last_dt = datetime.fromisoformat(last_started)
    except (ValueError, TypeError):
        return True  # 时间解析失败（脏数据 / 非 ISO 格式）→ 保守补跑
    now = datetime.now()
    yesterday_02 = (now - timedelta(days=1)).replace(hour=2, minute=0, second=0, microsecond=0)
    return last_dt < yesterday_02


# ============ lifespan：启动/销毁钩子 ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子（替代已废弃的 @app.on_event("startup")）

    职责：装配异步通知通道、载入数据湖、装配日志三路 handler。
    （因子注册表扫描已在 Phase 1·Task 3 随 factors 体系整体删除。）
    模块④（调度引擎）会在同一 lifespan 追加 scheduler 启动/关闭逻辑。
    """
    # 启动：装配异步通知通道（Telegram/企微/钉钉），缺凭证则跳过对应通道
    # Why 早于日志 handler：通知装配幂等且不依赖日志体系；先装配确保告警通道就绪，
    # 后续业务日志/风控事件即可被投递。build_default_manager 内部对缺凭证做软跳过。
    build_default_manager()

    # 启动：按 LAKE_CONFIG["lakes"] 多湖逐个 load（parquet 缺失则离线降级，不阻断启动）
    # Why 多湖而非单行：宏观 CTA 重构后数据体系分裂为 macro/sector/daily/minute/crypto
    # 五个独立 parquet（Task 3 已将 DataLakeReader 改为 {key:(df,ffill)} 多湖缓存），
    # 此处必须遍历 LAKE_CONFIG["lakes"] 逐 key 载入，首个成功 load 即为默认湖。
    # Why 缺失不阻断：load() 内部对 parquet 不存在仅记 warning 并 return（不写缓存），
    # 开发机/CI 缺数据湖时进入离线模式（.loaded=False，查询返回空 DF），保证 API 可启动。
    from data.lake_reader import DataLakeReader
    from config import LAKE_CONFIG
    reader = DataLakeReader.get_instance()
    for key, path in LAKE_CONFIG.get("lakes", {}).items():
        reader.load(path, key=key)

    # 启动：异步回测调度器（Spec 1 · Task 7）——ProcessPoolExecutor(concurrency=1) + daemon 调度线程
    # Why 寄生 uvicorn（零守护进程）：与 data_service sweep 同源——线程/子进程寄生主进程，非独立
    # Celery/PM2。worker initializer 自 load daily 湖（子进程独立内存空间），不依赖上面主进程 reader.load。
    # Why try/except 不阻断：装配失败（资源限制等）不应让整个 API 起不来——scheduler 缺席时
    # cancel 端点返 503，async 提交仍写 PENDING（下次启动调度器 poll 派发，不丢任务）。
    try:
        from concurrent.futures import ProcessPoolExecutor
        from backtest import tasks_db as replay_tasks_db
        from backtest import worker as replay_worker
        from backtest import scheduler as replay_scheduler
        replay_tasks_db.init_db()                       # 建表（幂等）
        app.state.replay_pool = ProcessPoolExecutor(
            max_workers=1, initializer=replay_worker._init_worker)   # concurrency=1 串行
        app.state.replay_scheduler = replay_scheduler.ReplayScheduler(
            app.state.replay_pool, {}, replay_tasks_db._DEFAULT_DB_PATH)
        app.state.replay_scheduler.start()
    except Exception:
        logging.getLogger(__name__).exception(
            "lifespan 装配异步回测调度器异常（已忽略，cancel 端点将返 503）"
        )

    # 启动：周度「近期回测」自动提交（daemon 线程，幂等）
    # Why（2026-08-03）：策略播报「近期回测」需要新鲜样本，但没有任何周期生产端
    # （前端写按钮已撤），曾停摆半个月。本线程寄生 uvicorn，每 6h 检查一次
    # replay_tasks 最近任务；距今 ≥7 天且无未终态任务时，提交「全市场 × 冠军参数
    # × 近 3 个月」回测任务，由上方 ReplayScheduler 异步派发执行（提交只 INSERT
    # 一行 PENDING，绝不阻塞）。
    try:
        import threading as _replay_threading

        def _weekly_replay_loop() -> None:
            import time as _replay_time
            from backtest.weekly_replay import maybe_enqueue_weekly_replay
            while True:
                try:
                    _tid = maybe_enqueue_weekly_replay()
                    if _tid:
                        logging.getLogger(__name__).info(
                            "已自动提交周度近期回测 task=%s", _tid)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "周度回测自动提交异常（吞掉继续）")
                _replay_time.sleep(6 * 3600)

        _replay_threading.Thread(
            target=_weekly_replay_loop, daemon=True,
            name="weekly-replay-enqueuer").start()
    except Exception:
        logging.getLogger(__name__).exception(
            "lifespan 装配周度回测提交线程异常（已忽略，播报新鲜度降级为只读）"
        )

    # 启动：训练 loop 编排器 + webhook 推报告 notifier（Spec 3 Task 7）
    # Why 寄生 uvicorn（合「零守护进程」哲学）：orchestrator daemon 线程寄生主进程，
    # 非独立 Celery/PM2。与上面 replay_scheduler / data sweep 同源。
    # （dws-migration Task 4 后不再起 dingtalk-stream 审核机器人；@审核改走 dws 桥，
    # 见下方装配块注释。）
    # Why try/except 不阻断：凭证缺/库异常不应让整个 API 起不来——orchestrator 缺席时 training
    # API 端点返 503/空状态，但 uvicorn 仍可起（其他业务不受影响）。重启恢复靠 reset_interrupted()
    # 把残留 RUNNING/ANALYZING → STOPPED（进程崩溃/重启时清理半成品 loop）。
    # 凭证软降级：REVIEW_* 未配 → _NoopNotifier（loop 可跑但无推送）。
    try:
        from backtest.optimize import training_loops_db
        from backtest.optimize.training_loop import TrainingLoopOrchestrator
        from backtest.optimize.training_dingtalk import (
            ReviewBotConfig,
            DingTalkNotifier,
            _NoopNotifier,
        )
        training_loops_db.init_db()                       # 建 training_loops 表（幂等）
        training_loops_db.reset_interrupted()             # 重启恢复：残留 RUNNING/ANALYZING → STOPPED
        _review_cfg = ReviewBotConfig.from_env()          # 凭证齐返 cfg，否则 None
        _notifier = DingTalkNotifier(_review_cfg) if _review_cfg is not None else _NoopNotifier()
        app.state.training_orchestrator = TrainingLoopOrchestrator(_notifier)
        app.state.training_orchestrator.start_daemon()    # daemon 线程跑 _loop 状态机
        # @审核消息已改走 dws dev connect 桥（dingtalk_review_bridge.py →
        # POST /api/v1/training/review），不再在此起 dingtalk-stream 审核机器人。
        # 此处仅装配 webhook 推报告 notifier + orchestrator daemon，@接收由 dws 桥负责。
    except Exception:
        logging.getLogger(__name__).exception(
            "lifespan 装配训练 loop 异常（已忽略，training API 将降级）"
        )

    # 启动：加载 symbol→企业名映射（#1，Tushare pro.stock_basic 全量，降级返 symbol）
    # Why 同步加载（非 daemon 线程）：stock_basic 一次 <1MB 快，且 list_plans 首请求需 symbol_name
    # 就绪；失败降级（get_name 返 symbol），不阻断启动。
    try:
        from data import symbol_names as _symbol_names
        _symbol_names.load_all()
    except Exception:
        logging.getLogger(__name__).warning("symbol_names load_all 异常", exc_info=True)

    # 启动：后台 daemon 线程扫 stale/missing 数据集，静默调 trigger_sync 补数据（#6）
    # Why daemon 线程不阻断启动：同步子进程是长任务（daily ~2.8h），线程异步跑；契合
    # config.py「零守护进程」（线程寄生主进程，非独立调度器如 Celery Beat/APScheduler）。
    # 复用 data_service.sweep_stale_on_startup（扫 list_datasets + trigger_sync 子进程+哨兵）。
    import threading as _threading
    from presentation.server.services import data_service as _data_service

    def _startup_sync_sweep() -> None:
        try:
            _triggered = _data_service.sweep_stale_on_startup()
            if _triggered:
                logging.getLogger(__name__).info(
                    "启动同步 sweep 触发：%s", _triggered)
        except Exception:
            logging.getLogger(__name__).warning(
                "启动同步 sweep 异常", exc_info=True)

    _threading.Thread(target=_startup_sync_sweep, daemon=True).start()

    # 启动：统一日志装配（三路并行：本地文件 + 前端 SSE 流 + 控制台）
    # Why 三路：本地文件事后排查无需复现（NaN 早抛/序列化失败留痕主阵地）；
    # 前端 SSE 流（RingBufferLogHandler→log_stream_hub→TerminalLogs）实时可观测；
    # 控制台由 uvicorn 自带 stdout handler 承担，此处不重复加。
    log_format = logging.Formatter(LOG_CONFIG["format"])
    # root setLevel：Python 默认 WARNING 会吞掉 INFO（业务链路打点主级别），
    # 必须显式放行到 LOG_CONFIG["level"]（默认 INFO），否则 service/engine 的
    # logger.info 既不进文件也不进前端流（test_logs_stream.py 的隐含契约）。
    root_logger = logging.getLogger()
    root_logger.setLevel(LOG_CONFIG["level"])

    # 本地文件 handler：自动建 logs/ 目录；事后定位 NaN/异常的核心证据来源
    import os as _os
    if os.environ.get("QUANTER_TESTING") == "1":
        # 测试隔离（08-06 用户拷问）：QUANTER_TESTING 下不挂生产文件 handler——
        # 否则 pytest 里任何真实 scheduler/logger 日志都会写进 logs/quanter.log，
        # 污染生产排查（实证：08:32 quanter.log 出现 pytest traceback）。
        logging.getLogger(__name__).info("QUANTER_TESTING=1：跳过本地文件日志 handler")
        app.state.log_file_handler = None
    else:
        _os.makedirs(_os.path.dirname(LOG_CONFIG["file"]), exist_ok=True)
        file_handler = logging.FileHandler(LOG_CONFIG["file"], encoding="utf-8")
        file_handler.setFormatter(log_format)
        file_handler.setLevel(LOG_CONFIG["level"])
        root_logger.addHandler(file_handler)
        app.state.log_file_handler = file_handler

    # 前端 SSE 流 handler（既有，保留 name|message 简格式供 TerminalLogs 展示）
    log_handler = RingBufferLogHandler(log_stream_hub)
    log_handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
    app.state.log_handler = log_handler
    root_logger.addHandler(log_handler)

    # C-2 scheduling-orchestration Task 9：TradingEngine 装配（合并 engine 进 uvicorn 单进程）。
    # 物理意图：原 ``python -m trading`` 独立常驻进程收编进本 lifespan——engine 与 server
    # 同进程后，四触发点 cron 在 uvicorn 内跑，data 采集经 ``pipeline_then_eod`` 事件链驱动
    # （取代 19:00 eod 时钟赌博）。dynamic_whitelist 物理隔离已由 W1 实例属性化完成
    # （engine._dynamic_whitelist，server 路径 submit_order 不读实例属性 → 两端输入源互不污染）。
    # Why try/except 不阻断：engine 装配失败（网关连不上 / state_store 建表失败 / 影子期不足）
    # 绝不应让整个 API 起不来——engine 缺席时交易 API 仍可用（手动下单路径不依赖 scheduler），
    # 仅自动 cron 编排缺席。与上面 replay_scheduler / training_orchestrator 同源软降级范式。
    try:
        from trading.engine import TradingEngine
        from trading.__main__ import check_shadow_gate, log_startup_banner
        # C-5 V2：装配 engine 前打启动 banner（session/account/mode/口径版本）。
        # 物理意图（spec §3.2 · [[qmt-connect-1-rootcause]]）：生产链 schtasks ONSTART→python -m trading→uvicorn
        # →lifespan 之前无 banner，session 漂移（进程内 123456 vs .env 123458）无日志可
        # 对比。banner 先于 bootstrap（含网关 connect）输出，便于排查 .env 漂移。
        log_startup_banner()
        eng = TradingEngine()
        await eng.bootstrap()
        if check_shadow_gate():
            eng.start()
            app.state.trading_engine = eng
            logging.getLogger(__name__).info("TradingEngine 已装配并启动")
        else:
            # 影子期不足（live 模式下 < TRADE_SHADOW_MIN_DAYS）：不 start scheduler，
            # 但保留实例供运维查看 / 手动 vet 戏（API 继续运行，engine 自动编排缺席）。
            app.state.trading_engine = eng
            logging.getLogger(__name__).warning(
                "TradingEngine 装配但 scheduler 未启动（影子期不足，API 继续运行）")
    except Exception:
        logging.getLogger(__name__).exception("TradingEngine 装配异常（已忽略）")

    # C-7 V1：broadcast connect 收编进 lifespan（5 CONNECT_BOTS）。
    # 物理意图（spec §3.1）：start_all step ② connect 编排移此处，软降级（单 bot
    # 失败不阻断 uvicorn）。live reload=False（C-5 V1）不 reload，connect 不抖动。
    # 与上方 engine/training_orchestrator 同源软降级范式——装配失败仅记日志不传播。
    # app.state.connect_bots：记录已起 bot，供 shutdown stop 对偶（树杀 dev connect +
    # Claude Code 子进程，防资源泄漏）。
    try:
        # A5（P1-2）：dev/测试实例不随服务器拉起 5 个 connect bot——dev.py 显式注入
        # QUANTER_DEV_SKIP_CONNECT_BOTS=1；pytest 用 QUANTER_TESTING=1。否则每个
        # dev 起停都带 5 个 bot 起停（08-05 日志 taskkill 30s 超时实证），且测试会
        # 拉起真 bot 污染环境。
        # QUANTER_DEV_MODE 是 dev.py 注入的「dev 实例」总开关（code-review：消除只写
        # 不读的死 env）；SKIP_CONNECT_BOTS 保留为更细粒度开关，任一命中即跳过。
        _skip_bots = (os.environ.get("QUANTER_TESTING") == "1"
                      or os.environ.get("QUANTER_DEV_MODE") == "1"
                      or os.environ.get("QUANTER_DEV_SKIP_CONNECT_BOTS") == "1")
        if _skip_bots:
            logging.getLogger(__name__).info(
                "lifespan 跳过 connect bot（QUANTER_TESTING / QUANTER_DEV_SKIP_CONNECT_BOTS=1）")
            app.state.connect_bots = []
        else:
            from broadcast.__main__ import CONNECT_BOTS, CONNECT_DEFAULTS
            from broadcast import connect_manager
            started_bots: list[str] = []
            for _bot in CONNECT_BOTS:            # cli/trading_q/data_q/strategy_q/review
                try:
                    connect_manager.start(_bot, CONNECT_BOTS[_bot], CONNECT_DEFAULTS)
                    started_bots.append(_bot)
                except RuntimeError:
                    # 配置缺失（unified_app_id 未填 / 身份闸缺失）→ 跳过该 bot，
                    # 同 broadcast.__main__._connect_start 语义（不让单点阻断其余）
                    logging.getLogger(__name__).warning(
                        "connect bot=%s 配置缺失跳过", _bot, exc_info=True)
                except Exception:
                    # 其它异常（subprocess 拉起失败等）→ 同样跳过，不阻断 uvicorn
                    logging.getLogger(__name__).exception(
                        "connect bot=%s 起异常（跳过，不阻断 uvicorn）", _bot)
            app.state.connect_bots = started_bots
    except Exception:
        # 外层兜底：CONNECT_BOTS import 失败等极端情况——已忽略，不阻断 lifespan
        logging.getLogger(__name__).exception("lifespan 装 connect 异常（已忽略）")
        app.state.connect_bots = []

    # C-7 V2：discovery 收编 lifespan（engine.sched cron 02:00 → subprocess 子进程）。
    # 物理意图（spec §3.2）：discovery 从 schtasks DAILY 02:00 收编到 engine.sched
    # AsyncIOScheduler，触发 _run_discovery_subprocess（DETACHED 子进程跑 cli daemon）。
    # 软降级：engine 未装配（None）/ 影子期未 start sched / add_job 抛异常 → 跳过，
    # 不阻断 uvicorn（与上方 engine/training/connect 同源软降级范式）。
    # Why getattr 防御：engine 装配块 try/except 隔离，极端失败时 state 上可能无
    # trading_engine——cron 注册必须对「未装配」也安全。
    try:
        _eng = getattr(app.state, "trading_engine", None)
        if _eng is not None:
            import os as _os_cron
            from apscheduler.triggers.cron import CronTrigger
            # 24h 低功率（2026-08-03）：DISCOVERY_SCHEDULE=low-power → 每小时整点+5 分
            # 触发，job 内窗口判定（盘中/数据链时段跳过）；否则保持 02:00 夜间集中跑。
            if _os_cron.environ.get("DISCOVERY_SCHEDULE", "").lower() == "low-power":
                _eng.sched.add_job(
                    _run_discovery_subprocess,
                    CronTrigger.from_crontab("5 * * * *"),
                    id="discovery_daemon",
                    replace_existing=True,
                )
                logging.getLogger(__name__).info(
                    "discovery cron 每小时+5 分已注册到 engine.sched（24h 低功率模式）")
            else:
                _eng.sched.add_job(
                    _run_discovery_subprocess,
                    CronTrigger(hour=2, minute=0),
                    id="discovery_daemon",
                    replace_existing=True,
                )
                logging.getLogger(__name__).info(
                    "discovery cron 02:00 已注册到 engine.sched")
    except Exception:
        logging.getLogger(__name__).exception(
            "lifespan 装 discovery cron 异常（已忽略）")

    # research digest 周期推送 cron（2026-08-03 · Agent 观察环推送腿）。
    # 默认每交易日 18:30（pipeline_then_eod 18:00 之后，数据/回测期望已就绪），
    # env RESEARCH_DIGEST_CRON 可覆盖（crontab 5 段格式）。软降级同 discovery cron。
    try:
        _eng_digest = getattr(app.state, "trading_engine", None)
        if _eng_digest is not None:
            import os as _os_cron
            from apscheduler.triggers.cron import CronTrigger
            _digest_cron = _os_cron.environ.get("RESEARCH_DIGEST_CRON", _DIGEST_CRON_DEFAULT)
            _eng_digest.sched.add_job(
                _run_research_digest_push,
                CronTrigger.from_crontab(_digest_cron),
                id="research_digest_push",
                replace_existing=True,
            )
            logging.getLogger(__name__).info(
                "research digest cron %s 已注册到 engine.sched", _digest_cron)
    except Exception:
        logging.getLogger(__name__).exception(
            "lifespan 装 research digest cron 异常（已忽略）")

    # C-7 V3：discovery 启动补跑（offline 容错，收编自洽必需）。
    # 物理意图（spec §3.3）：offline 跨昨晚 02:00 → 启动补跑（DETACHED subprocess，
    # 不阻塞 uvicorn 起）。幂等靠 discovery 既有轮次/seed + run_daemon_cycle 早退
    # （converged 跳过）去重——补跑 + 当晚 02:00 双跑靠此去重，不会重跑已收敛 snapshot。
    # 软降级：补跑判定 / 子进程拉起异常不阻断 uvicorn（try/except 兜底，与上方 engine/
    # training/connect/discovery cron 同源软降级范式）。
    try:
        if _discovery_missed_last_run():
            logging.getLogger(__name__).warning(
                "discovery 启动补跑：检测到 offline 跨昨晚 02:00，异步补跑")
            _run_discovery_subprocess()  # DETACHED 子进程，立即返不阻塞 uvicorn 起
    except Exception:
        logging.getLogger(__name__).exception("discovery 启动补跑异常（不阻断 uvicorn）")

    # C-8 V5：全 job 启动补跑（台账 + 后台编排，spec §3.6）。
    # 物理意图：生产机不 7x24，offline 跨 18:00 pipeline / 09:22 pre_open 时，启动补跑
    # 到「当前可用的一致态」（采集→data_ready→eod→brief + pre_open 窗口内补挂）。
    # 仅 engine 已 start（sched.running）时触发——影子期不足 scheduler 缺席，补跑无意义。
    # 软降级：创建异常不阻断 uvicorn（与上方 engine/training/connect/discovery 同范式）。
    try:
        from trading.catchup import run_startup_catchup
        _eng_c8 = getattr(app.state, "trading_engine", None)
        if _eng_c8 is not None and getattr(_eng_c8.sched, "running", False):
            app.state.catchup_task = asyncio.create_task(run_startup_catchup(_eng_c8))
            logging.getLogger(__name__).info("C-8 启动补跑任务已创建")
    except Exception:
        logging.getLogger(__name__).exception("C-8 启动补跑创建异常（已忽略）")

    yield
    # C-8 V5：shutdown 取消启动补跑任务（软降级；任务随事件循环销毁自然结束，
    # 显式 cancel 更干净——采集子进程 await proc.wait() 会随事件循环关闭被中断）。
    _cu_task = getattr(app.state, "catchup_task", None)
    if _cu_task is not None:
        try:
            _cu_task.cancel()
        except Exception:
            logging.getLogger(__name__).exception("C-8 启动补跑任务 cancel 异常（已忽略）")


    # 销毁：优雅断开交易网关（B-18）——logout 释放券商会话，防进程退出时连接泄漏。
    # Why try/except 吞异常：shutdown 路径不应因网关断开失败而阻塞后续 handler 清理；
    # 无网关装配（开发态/CI）时 get_gateway 返 None，直接跳过。
    try:
        from trading.gateway_service import get_gateway
        gw = get_gateway()
        if gw is not None:
            await gw.disconnect()
    except Exception:
        logging.getLogger(__name__).exception(
            "lifespan shutdown 断开交易网关异常（已忽略，继续清理日志 handler）"
        )

    # C-7 V1：shutdown 树杀 connect bots（与 startup start 对偶）。
    # 物理意图：startup 起的 dev connect 常驻进程（含其拉起的 Claude Code 子进程），
    # shutdown 时必须 taskkill /F /T 树杀，防进程退出后孤儿 Claude Code 实例持续吃资源。
    # Why getattr 防御：startup 装配块 try/except 隔离，极端失败时 state 上可能无
    # connect_bots——shutdown 路径必须对「未装配」也安全。
    # Why try/except 包每 bot：单 bot stop 失败（taskkill 超时等）不阻断其余 bot 的树杀。
    for _bot in getattr(app.state, "connect_bots", []):
        try:
            from broadcast import connect_manager
            connect_manager.stop(_bot)          # taskkill /F /T 树杀
        except Exception:
            logging.getLogger(__name__).exception(
                "shutdown connect bot=%s 异常（已忽略）", _bot)

    # 销毁：TradingEngine scheduler（Task 9 · 合并 engine 进 uvicorn 后的优雅停机）。
    # Why getattr 防御：lifespan 装配块 try/except 隔离，装配失败 / 影子期未 start 时
    # state 上可能无 trading_engine 或 sched 未起——shutdown 路径必须对「未装配/未启动」
    # 也安全（不能因 engine 装配失败而让整个 shutdown 崩，与 training_orchestrator 同范式）。
    # sched.running 由 APScheduler 维护：start() 后 True，shutdown() 后 False——据此判定
    # 是否需要调 shutdown，避免对未启动 scheduler 调 shutdown 抛 SchedulerNotRunningError。
    _eng = getattr(app.state, "trading_engine", None)
    if _eng is not None and getattr(_eng.sched, "running", False):
        _eng.shutdown()

    # 销毁：卸载日志 handler（前端流 + 本地文件），避免重复挂载/引用泄漏
    # （reload 或测试复用进程时关键，否则 handler 单调累积致日志重复输出）
    root_logger = logging.getLogger()
    root_logger.removeHandler(app.state.log_handler)
    if getattr(app.state, "log_file_handler", None) is not None:
        root_logger.removeHandler(app.state.log_file_handler)
    # 销毁：异步回测调度器（Spec 1 · Task 7）——停调度线程 + 关进程池（不等在跑回测，最快退出）
    _sched = getattr(app.state, "replay_scheduler", None)
    if _sched is not None:
        _sched.stop()
    _pool = getattr(app.state, "replay_pool", None)
    if _pool is not None:
        _pool.shutdown(wait=False)
    # 销毁：训练 loop daemon（Spec 3 Task 7）。@审核 stream 已在 dws-migration Task 4 删除，
    # 不再有 review_bot_task 需要 cancel，shutdown 仅停 orchestrator daemon 线程。
    # Why getattr 防御：lifespan 装配块 try/except 隔离，装配失败时 state 上无此属性；
    # shutdown 路径必须对「未装配」也安全（不能因 training 装配失败而让整个 shutdown 崩）。
    # stop_daemon 设 daemon 线程 stop 标志（线程自行退出，不 join 阻塞 uvicorn 退出）。
    _orch = getattr(app.state, "training_orchestrator", None)
    if _orch is not None:
        _orch.stop_daemon()


# ============ 创建应用 ============
app = FastAPI(
    title="Quanter 量化回测平台",
    description=(
        "量化交易驾驶舱 API：宏观/板块/数据湖只读视图 + 实盘交易 + AI 复盘。"
        "（HMM 组合回测已在蔡森专精化 Phase 1·Task 5 移除）"
    ),
    version="2.0.0",
    lifespan=lifespan,
    # 同步端点 NaN 早抛防线：StrictJSONResponse 用 allow_nan=False，任何漏标量化
    # 的路径在这里暴露（500 + 中文错误），而非把字面 NaN 推给前端静默吞。
    # 与 SSE 流式端点的 sse_dumps 对称（见 server/api/v1/_sse.py）。
    default_response_class=StrictJSONResponse,
)

# ============ 注册 CORS 中间件 ============
# 开发阶段允许前端 Vite dev server 跨域访问后端 API
# 【B-1】allow_methods 收敛为实际使用的谓词（不再 "*"，配合 allow_credentials=True
# 缩小跨域攻击面）；allow_origins 读 CORS_ORIGINS 白名单（仅本地 dev 端口）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],      # 允许所有请求头（含 Authorization Bearer）
)

# ============ 挂载路由 ============
# API 版本化前缀：/api/v1/
app.include_router(logs_router, prefix="/api/v1")
# 宏观/板块/因子只读端点：四端点全部只读内存湖，无网络/无写入，
# 缺数据湖时端点内部短路返空结构（离线降级），不阻断 lifespan。
app.include_router(macro_router, prefix="/api/v1")
# 实盘交易路由（优雅降级真接 QMT；lifespan 不自动 connect，单例 lazy 构造）
# 【B-1】路由级鉴权：下单/熔断/连接等敏感端点强制 require_write（token 未配置=开发放行）。
app.include_router(trading_router, prefix="/api/v1", dependencies=[Depends(require_write)])
# AI 参数训练 loop（Spec 3 Task 7）：start/get/list/submit_review 四端点。
# 训练提交/审核提交是写操作（落库 + 触发回测子进程），路由级鉴权保护；
# 钉钉审核 handler 进程内调 orchestrator 不走 HTTP，不受 require_write 限制。
app.include_router(training_router, prefix="/api/v1", dependencies=[Depends(require_write)])
# 数据湖资产（层级一）：纯字典注册表 + 文件系统状态推导，零守护进程，不阻断 lifespan
# sync 端点可起同步子进程/落盘，路由级鉴权保护。
app.include_router(data_router, prefix="/api/v1", dependencies=[Depends(require_write)])
# AI 复盘（层级六）：GLM 调用 + 三级降级（缺凭证/调用失败/无数据均不阻断）
# diagnose 触发外部 LLM 调用（成本/滥用面），路由级鉴权保护。
app.include_router(review_router, prefix="/api/v1", dependencies=[Depends(require_write)])
app.include_router(research_router, prefix="/api/v1", dependencies=[Depends(require_write)])
app.include_router(ops_router, prefix="/api/v1", dependencies=[Depends(require_write)])


# ============ 健康检查端点 ============
@app.get("/health", summary="健康检查", tags=["系统"])
async def health_check():
    """
    健康检查端点

    用于前端/运维确认后端服务存活。
    返回服务状态和版本信息。
    """
    return {
        "status": "ok",
        "service": "quanter-api",
        "version": "2.0.0",
    }
