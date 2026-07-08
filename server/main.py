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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.core.config import CORS_ORIGINS, LOG_CONFIG
from server.core._responses import StrictJSONResponse
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

    # 销毁：卸载日志 handler（前端流 + 本地文件），避免重复挂载/引用泄漏
    # （reload 或测试复用进程时关键，否则 handler 单调累积致日志重复输出）
    root_logger = logging.getLogger()
    root_logger.removeHandler(app.state.log_handler)
    root_logger.removeHandler(app.state.log_file_handler)
    # 销毁：模块④在此追加 scheduler.shutdown()


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
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],      # 允许所有 HTTP 方法
    allow_headers=["*"],      # 允许所有请求头
)

# ============ 挂载路由 ============
# API 版本化前缀：/api/v1/
app.include_router(logs_router, prefix="/api/v1")
# 宏观/板块/因子只读端点：四端点全部只读内存湖，无网络/无写入，
# 缺数据湖时端点内部短路返空结构（离线降级），不阻断 lifespan。
app.include_router(macro_router, prefix="/api/v1")
# 实盘交易路由（优雅降级真接 QMT；lifespan 不自动 connect，单例 lazy 构造）
app.include_router(trading_router, prefix="/api/v1")
# 数据湖资产（层级一）：纯字典注册表 + 文件系统状态推导，零守护进程，不阻断 lifespan
app.include_router(data_router, prefix="/api/v1")
# AI 复盘（层级六）：GLM 调用 + 三级降级（缺凭证/调用失败/无数据均不阻断）
app.include_router(review_router, prefix="/api/v1")


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
