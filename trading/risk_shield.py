# -*- coding: utf-8 -*-
"""trading/risk_shield.py — 垫片 re-export（Layer2 阶段2 · strangler 铁律①）。

物理定义（check_order + RiskDecision）已迁至 ``trading/compute/risk.py``（functional
core）。本文件仅做 re-export 转发，保证既有 ``from trading.risk_shield import check_order,
RiskDecision`` 调用零改动继续可用。

迁移路径：
    trading/risk_shield.py（旧物理定义） ──搬迁──▶ trading/compute/risk.py（新源）
                                         ◀──垫片 re-export── 本文件

is 同源契约：``trading.compute.risk.check_order`` 与 ``trading.risk_shield.check_order``
与 ``execution.check_order`` 三入口指向同一函数对象（见 tests/test_compute_purity.py）。
"""
from __future__ import annotations

from trading.compute.risk import (  # noqa: F401
    check_order,
    RiskDecision,
)

__all__ = ["check_order", "RiskDecision"]
