# -*- coding: utf-8 -*-
"""
FastAPI 应用入口

职责：
1. 创建 FastAPI 应用实例
2. 注册 CORS 中间件（允许前端 Vite dev server 跨域访问）
3. 挂载 API 路由（/api/v1/logs, /api/v1/trading 等）
4. 提供健康检查端点

启动方式：
    uvicorn server.main:app --reload --host 0.0.0.0 --port 8000

设计原则：
- 应用入口仅做组装，不包含业务逻辑
- CORS 配置从 core/config.py 读取，不硬编码
- 路由版本化 /api/v1/，预留后续版本空间
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.core.config import CORS_ORIGINS, LOG_CONFIG
from server.core._responses import StrictJSONResponse
# API 鉴权依赖（B-1）：挂在敏感 router（trading/caisen/data/review）上，
# token 未配置=开发态放行（WARNING），生产须配 QUANTER_API_TOKEN。
from server.core.auth import require_write
from server.api.v1.logs import (
    RingBufferLogHandler,
    log_stream_hub,
    router as logs_router,
)
# 宏观/板块/因子只读端点（T16）：读内存湖 + CreditRegime，零写入，
# 供给前端驾驶舱（T17 /dashboard）宏观灯/信贷曲线/板块流/ATR 四视图。
from server.api.v1.macro import router as macro_router
# 实盘交易（优雅降级真接 QMT；无 xtquant/缺凭证时 /status 返 unavailable，不阻断 lifespan）
from server.api.v1.trading import router as trading_router
# 蔡森形态学流水线 REST 路由（Phase 3 Task 4）：scan/plans/activate/chart/positions/replay，
# 调 caisen_service 编排层 + storage 持久化，异常三类（KeyError→404/ValidationError→422/
# ValueError→422）路由层转译。NaN 经 StrictJSONResponse 早抛。
from server.api.v1.caisen import router as caisen_router
# 数据湖资产路由（层级一）：扫描 parquet mtime + 哨兵推导状态，触发同步起 daemon 子进程
from server.api.v1.data import router as data_router
# AI 复盘路由（层级六）：GLM 调用 + 三级降级，CPU/网络阻塞走线程池
from server.api.v1.review import router as review_router
# 通知装配：Telegram/企微/钉钉三通道按凭证装配，缺凭证跳过对应通道
from core.notifier import build_default_manager

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
        from caisen import replay_tasks_db, replay_worker, replay_scheduler
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
    from server.services import data_service as _data_service

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

    yield

    # 销毁：优雅断开交易网关（B-18）——logout 释放券商会话，防进程退出时连接泄漏。
    # Why try/except 吞异常：shutdown 路径不应因网关断开失败而阻塞后续 handler 清理；
    # 无网关装配（开发态/CI）时 get_gateway 返 None，直接跳过。
    try:
        from server.services.trading_service import get_gateway
        gw = get_gateway()
        if gw is not None:
            await gw.disconnect()
    except Exception:
        logging.getLogger(__name__).exception(
            "lifespan shutdown 断开交易网关异常（已忽略，继续清理日志 handler）"
        )

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
# 蔡森形态学流水线 REST 路由（Phase 3 Task 4）：7 端点 + 异常映射（KeyError→404/
# ValidationError→422/ValueError→422），算法/IO 异常 service 层已降级返空结果。
# 含 scan/activate/审核等可触发真实下单流程的端点，路由级鉴权保护。
app.include_router(caisen_router, prefix="/api/v1", dependencies=[Depends(require_write)])
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
