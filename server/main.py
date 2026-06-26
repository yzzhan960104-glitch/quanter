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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.core.config import CORS_ORIGINS
from server.api.v1.backtest import router as backtest_router
from server.api.v1.portfolio import router as portfolio_router

# ============ 创建应用 ============
app = FastAPI(
    title="Quanter 量化回测平台",
    description=(
        "基于 HMM 宏观状态识别的多资产组合回测 API。"
        "支持单资产信号回测和多资产组合调仓回测两种模式。"
    ),
    version="1.0.0",
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
        "version": "1.0.0",
    }
