# -*- coding: utf-8 -*-
"""回测内核指纹（单源）：ENGINE_FILES 清单 + engine_hash()。

出身：2026-08-18 compute_unit 退役（ADR-17）时从 compute_unit/hashes.py 逐字节迁入
——算法与清单一项未动，迁移前后输出恒等（退役 spec G1 留证 ce16cc4ee4de），
trials 库既有 engine_hash 全程有效，老 trial 可比性不受退役影响。

物理意图：内核任一文件一动，engine_hash 变，老 trial 自然与新跑不可比（stale
判定依据）。调用方：discovery/runner._engine_hash（薄委托）、discovery/cli（经
runner）、diag 证据脚本（engine_hash 留痕于报告）。

清单演进史：P1-3（2026-08-02）从 backtest+method_v0 两文件扩到完整内核；T18
（2026-08-15）补 price_levels.py（价位数学单源是 backtest 传递依赖）。

⚠️ 本模块自身不入 ENGINE_FILES（与 compute_unit/hashes.py 时期对称）——改指纹
实现/注释不改变 engine_hash，不误伤老 trial 可比性。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# 项目根（discovery/ 的上级 = quanter/）
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# 回测内核文件清单（指纹覆盖面）。元组次序是 hash 输入的一部分，不得重排。
ENGINE_FILES = (
    "strategies/neckline/backtest.py",
    # T18（2026-08-15 · T7 强制遗留收口）：price_levels.py 现承载回测内核的价位数学
    # （compute_price_levels/PRICE_LEVEL_DEFAULTS），是 backtest.simulate_exit 的传递依赖
    # （backtest.py `from .price_levels import ...`）。不在清单 = 指纹盲区：改价位公式
    # （止损基准/tp 倍数口径）engine_hash 不变，老 trial 不标 stale——回测⇄实盘等价性
    # 头号资产的守门人自己漏了门。加入即 hash 变化属预期重估。
    "strategies/neckline/price_levels.py",
    "strategies/neckline/method_v0.py",
    "strategies/neckline/strategy.py",
    "strategies/neckline/execution.py",
    "strategies/neckline/signal.py",
    "backtest/replay.py",
    "backtest/models.py",
    "discovery/objective.py",
)


def engine_hash() -> str:
    """回测内核指纹：ENGINE_FILES 逐文件内容 sha256[:12]（文件名入 hash 防改名漏检）。"""
    h = hashlib.sha256()
    for rel in ENGINE_FILES:
        h.update(rel.encode("utf-8"))
        with open(PROJECT_ROOT / rel, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]
