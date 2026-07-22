# -*- coding: utf-8 -*-
"""trading/stop_loss.py — 垫片 re-export（Layer2 阶段2 · strangler 铁律①）。

物理定义（compute_stop_price）已迁至 ``trading/compute/stop.py``（functional core）。
本文件仅做 re-export 转发，保证既有 ``from trading.stop_loss import compute_stop_price``
调用零改动继续可用。

is 同源契约：``trading.compute.stop.compute_stop_price`` 与
``trading.stop_loss.compute_stop_price`` 两入口指向同一函数对象。
"""
from __future__ import annotations

from trading.compute.stop import compute_stop_price  # noqa: F401

__all__ = ["compute_stop_price"]
