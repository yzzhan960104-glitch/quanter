# -*- coding: utf-8 -*-
"""trading/signal_runner.py — 垫片 re-export（Layer2 阶段2 · strangler 铁律①）。

物理定义（build_orders_from_signals + PlannedOrder）已迁至 ``trading/compute/plan.py``
（functional core）。本文件仅做 re-export 转发，保证既有
``from trading.signal_runner import build_orders_from_signals, PlannedOrder`` 调用
零改动继续可用。

迁移路径：
    trading/signal_runner.py（旧物理定义） ──搬迁──▶ trading/compute/plan.py（新源）
                                           ◀──垫片 re-export── 本文件

is 同源契约：``trading.compute.plan.build_orders_from_signals`` 与
``trading.signal_runner.build_orders_from_signals`` 两入口指向同一函数对象。
"""
from __future__ import annotations

from trading.compute.plan import (  # noqa: F401
    build_orders_from_signals,
    PlannedOrder,
)

__all__ = ["build_orders_from_signals", "PlannedOrder"]
