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
import logging
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
# 通知装配：Telegram/企微/钉钉三通道按凭证装配，缺凭证跳过对应通道
from infra.notifier import build_default_manager

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
        # 物理意图（spec §3.2 · [[qmt-connect-1-rootcause]]）：生产链 start_all→uvicorn
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
        from broadcast.__main__ import CONNECT_BOTS, CONNECT_DEFAULTS
        from broadcast import connect_manager
        started_bots: list[str] = []
        for _bot in CONNECT_BOTS:                # cli/trading_q/data_q/strategy_q/review
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

    yield

    # 销毁：优雅断开交易网关（B-18）——logout 释放券商会话，防进程退出时连接泄漏。
    # Why try/except 吞异常：shutdown 路径不应因网关断开失败而阻塞后续 handler 清理；
    # 无网关装配（开发态/CI）时 get_gateway 返 None，直接跳过。
    try:
        from presentation.server.services.trading_service import get_gateway
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
