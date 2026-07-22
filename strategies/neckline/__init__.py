# -*- coding: utf-8 -*-
"""颈线法策略子包（Layer2 解耦·Task 1.5 收口）。

物理定位：
    颈线法算法本体（识别层 method_v0 + 回测执行层 backtest）原散落在 scripts/ 下，
    靠 sys.path.insert hack 挂载。本子包把它们收口进 strategies/neckline/ 正式包，
    消除 sys.path hack，import 路径与策略适配器同包同级。

    决策逻辑零改动（纯文件归位 + import 改写），T1 golden per_symbol 数值逐位不变。

子模块：
    method_v0：颈线形态识别（detect_neckline_method / compute_atr / search_neckline
               / local_minima / local_maxima / DEFAULTS）
    backtest：持有期模拟与回测（simulate_exit / dedup_signals / kelly_metrics /
              risk_metrics / scan_symbol / EXEC_DEFAULTS）

公开符号统一从此处 re-export（消费方 `from strategies.neckline import ...`），
避免直接 reach into .method_v0 / .backtest（保留子模块内部分层的语义）。
"""
from .method_v0 import (
    DEFAULTS,
    compute_atr,
    local_minima,
    local_maxima,
    search_neckline,
    detect_neckline_method,
)
from .backtest import (
    MAX_HOLDING,
    MAX_WAIT,
    COOLDOWN,
    TOP_N,
    EXEC_DEFAULTS,
    simulate_exit,
    dedup_signals,
    kelly_metrics,
    risk_metrics,
    scan_symbol,
)

__all__ = [
    # method_v0（识别层）
    "DEFAULTS",
    "compute_atr",
    "local_minima",
    "local_maxima",
    "search_neckline",
    "detect_neckline_method",
    # backtest（执行层）
    "MAX_HOLDING",
    "MAX_WAIT",
    "COOLDOWN",
    "TOP_N",
    "EXEC_DEFAULTS",
    "simulate_exit",
    "dedup_signals",
    "kelly_metrics",
    "risk_metrics",
    "scan_symbol",
]
