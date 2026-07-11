# -*- coding: utf-8 -*-
"""
FastAPI 后端核心配置

职责：
1. CORS 跨域白名单（开发环境允许前端 localhost 访问）
2. 数据源默认参数（MockDataFetcher 种子）
3. 本地日志装配（level/format/file，不含敏感信息）

设计原则：
- 纯 Python 字典配置，不引入 pydantic-settings 等重型配置框架
- 开发/生产环境通过环境变量切换（后续扩展）
"""
import sys
import os
from pathlib import Path
from typing import Dict, Any, List

# ============ 项目根目录 ============
# server/core/config.py → server/ → 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 将项目根目录加入 sys.path，确保 import core / data / viz 等模块可用
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ============ CORS 跨域配置 ============
# 开发阶段允许前端 Vite dev server 访问后端 API
CORS_ORIGINS: List[str] = [
    "http://localhost:5173",   # Vite 默认端口
    "http://localhost:3000",   # 备选端口
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
]

# ============ 服务监听配置 ============
# 后端 uvicorn 监听地址与端口——前后端端口的**单一真相源**：被 server/main.py 的
# __main__ 块与 scripts/check_ports.py（前端 npm run dev 的 predev 护栏）共同引用，
# 杜绝 web/vite.config.ts 的 proxy target 与后端端口漂移（曾出现 8001 vs 8000 的
# ECONNREFUSED 事故）。env 可覆盖：API_HOST / API_PORT（容器/CI 内需改端口时）。
API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int = int(os.getenv("API_PORT", "8000"))

# ============ 数据源默认参数 ============
DATA_DEFAULTS: Dict[str, Any] = {
    "mock_seed": 42,            # MockDataFetcher 随机种子（确保可复现）
    "default_timezone": "Asia/Shanghai",
}

# ============ 本地日志配置 ============
# 设计意图：Python root logger 默认级别 WARNING 会吞掉 INFO（业务链路打点的
# 主要级别），必须显式 setLevel(INFO) 才能放行；同时落盘一份本地文件，便于
# 事后排查无需复现。日志记录经 RingBufferLogHandler 同时流到前端 TerminalLogs
#（见 server/main.py lifespan 装配），本地文件 + 前端流 + 控制台三路并行。
# 凭证隔离红线：此处不含任何敏感信息，仅格式/级别/路径，可安全提交。
LOG_CONFIG: Dict[str, Any] = {
    # 默认 INFO（放行业务链路打点）；可经 LOG_LEVEL 环境变量提级到 DEBUG 排查
    "level": os.getenv("LOG_LEVEL", "INFO"),
    # 时间 | 级别(7列对齐) | logger名 | 消息 —— 与 server/api/v1/logs.py 的
    # RingBufferLogHandler formatter 风格一致，便于本地文件与前端日志交叉比对
    "format": "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    # 本地落盘路径（项目根/logs/quanter.log）；启动时自动建目录
    "file": str(PROJECT_ROOT / "logs" / "quanter.log"),
}
