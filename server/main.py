# -*- coding: utf-8 -*-
"""
FastAPI 应用入口

职责：
1. 创建 FastAPI 应用实例
2. 注册 CORS 中间件（允许前端 Vite dev server 跨域访问）
3. 挂载 API 路由（/api/v1/backtest, /api/v1/portfolio）
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

from server.core.config import CORS_ORIGINS
from server.api.v1.backtest import router as backtest_router
from server.api.v1.portfolio import router as portfolio_router
from server.api.v1.logs import (
    RingBufferLogHandler,
    log_stream_hub,
    router as logs_router,
)
from strategies.loader import StrategyLoader
from server.api.v1.strategies import router as strategies_router
from server.api.v1.explorer import router as explorer_router
# 通知装配：Telegram/企微/钉钉三通道按凭证装配，缺凭证跳过对应通道
from core.notifier import build_default_manager

# ============ lifespan：启动/销毁钩子 ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子（替代已废弃的 @app.on_event("startup")）

    Why 集中扫描：策略注册表进程内不变，启动期 importlib 一次性扫描写入
    app.state.strategy_loader，后续 API 路由只读，避免每请求重复扫描。
    模块④（调度引擎）会在同一 lifespan 追加 scheduler 启动/关闭逻辑。
    """
    # 启动：扫描策略注册到 app.state
    loader = StrategyLoader()
    loader.scan()
    app.state.strategy_loader = loader

    # 启动：装配异步通知通道（Telegram/企微/钉钉），缺凭证则跳过对应通道
    # Why 早于日志 handler：通知装配幂等且不依赖日志体系；先装配确保告警通道就绪，
    # 后续业务日志/风控事件即可被投递。build_default_manager 内部对缺凭证做软跳过。
    build_default_manager()

    # 启动：数据湖常驻内存（parquet 缺失则离线降级，不阻断启动）
    # Why 单例 + 启动期加载：DataLakeReader.load() 内部对 parquet 缺失只记 warning，
    # 开发机/CI 无数据湖时进入离线模式（查询返回空 DF），绝不抛异常阻断 API 启动。
    from data.lake_reader import DataLakeReader
    DataLakeReader.get_instance().load()

    # 启动：GLM 客户端单例（凭证缺失则降级中性，不阻断启动）
    # Why 放在 LakeReader 之后：GLM 情感因子属另类数据，与行情数据湖无依赖，
    # 单例 __init__ 仅读 env + 建 AsyncOpenAI client 句柄（不发请求），
    # 缺 ZHIPU_API_KEY 时 _client=None 进入降级模式，后续 analyze_sentiment
    # 一律返回中性——保证开发机/CI 无凭证也能正常起服务。
    from core.llm_client import GLMClient
    GLMClient.get_instance()

    # 启动：挂载 SSE 日志 handler 到 root logger
    # Why root logger：回测业务线程的全部日志（含第三方库）都需被捕获，只有 root
    # 能拦截子 logger 的向上传播记录，确保 SSE 流完整。
    log_handler = RingBufferLogHandler(log_stream_hub)
    log_handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
    app.state.log_handler = log_handler
    logging.getLogger().addHandler(log_handler)

    yield

    # 销毁：卸载日志 handler，避免重复挂载/引用泄漏（reload 或测试复用进程时关键）
    logging.getLogger().removeHandler(app.state.log_handler)
    # 销毁：模块④在此追加 scheduler.shutdown()


# ============ 创建应用 ============
app = FastAPI(
    title="Quanter 量化回测平台",
    description=(
        "基于 HMM 宏观状态识别的多资产组合回测 API。"
        "支持单资产信号回测和多资产组合调仓回测两种模式。"
    ),
    version="2.0.0",
    lifespan=lifespan,
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
app.include_router(backtest_router, prefix="/api/v1")
app.include_router(portfolio_router, prefix="/api/v1")
app.include_router(strategies_router, prefix="/api/v1")
app.include_router(logs_router, prefix="/api/v1")
# 因子探索沙盒（Celery 派发 + Redis 宕机降级）：内部对 Redis 不可用做了
# fire_and_forget 告警 + 线程池降级，无 Redis 也能挂载、不阻断 lifespan。
app.include_router(explorer_router, prefix="/api/v1")


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
