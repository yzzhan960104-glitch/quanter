# -*- coding: utf-8 -*-
"""
FastAPI 后端核心配置

职责：
1. CORS 跨域白名单（开发环境允许前端 localhost 访问）
2. 数据源默认参数（MockDataFetcher 种子）
3. 回测引擎默认参数（与 config.py 对齐，但此处面向 API 层）

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

# 将项目根目录加入 sys.path，确保 import backtest / factors / data 等模块可用
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

# ============ 数据源默认参数 ============
DATA_DEFAULTS: Dict[str, Any] = {
    "mock_seed": 42,            # MockDataFetcher 随机种子（确保可复现）
    "default_timezone": "Asia/Shanghai",
}

# ============ 单资产回测默认参数 ============
# 与项目根目录 config.py 中的 BACKTEST_CONFIG / FACTOR_CONFIG 对齐
BACKTEST_DEFAULTS: Dict[str, Any] = {
    "initial_capital": 1_000_000,
    "signal_freq": "1d",
    "tech_weights": {"tech": 0.7, "macro": 0.3},
    "cost_model": {
        "commission_rate": 0.0003,
        "stamp_duty": 0.0005,
        "min_commission": 5.0,
        "slippage_model": "linear",
        "slippage_rate": 0.001,
        "liquidity_threshold": 0.02,
    },
}

# ============ 组合回测默认参数 ============
PORTFOLIO_DEFAULTS: Dict[str, Any] = {
    "initial_capital": 1_000_000,
    "n_hmm_states": 3,
    "buffer_threshold": 0.05,
    "hmm_covariance_type": "diag",
    "hmm_n_iter": 100,
    "hmm_random_state": 42,
    # 默认组合：沪深300 ETF + 国债 ETF
    "symbols": ["510300.SH", "511010.SH"],
    "state_weights": {
        "State_0": {"510300.SH": 0.8, "511010.SH": 0.2},  # 扩张期
        "State_1": {"510300.SH": 0.2, "511010.SH": 0.8},  # 衰退期
        "State_2": {"510300.SH": 0.5, "511010.SH": 0.5},  # 平稳期
    },
}

# ============ API 性能配置 ============
API_CONFIG: Dict[str, Any] = {
    "backtest_timeout": 60,     # 单资产回测超时（秒）
    "portfolio_timeout": 120,   # 组合回测超时（秒），HMM 训练更耗时
}
