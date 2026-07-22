# -*- coding: utf-8 -*-
"""execution/exit_logic.py — 垫片 re-export（Layer2 阶段2 · strangler 铁律①）。

物理定义已迁至 ``trading/compute/exit.py``（functional core）。本文件仅做 re-export
转发，保证既有 ``from execution.exit_logic import check_exit`` 调用零改动继续可用。

迁移路径：
    execution/exit_logic.py（旧物理定义） ──git mv──▶ trading/compute/exit.py（新源）
                                            ◀──垫片 re-export── 本文件

is 同源契约：``trading.compute.exit.check_exit`` 与 ``execution.exit_logic.check_exit``
与 ``execution.check_exit`` 三入口指向同一函数对象（见 tests/test_compute_purity.py）。
"""
from __future__ import annotations

from trading.compute.exit import (  # noqa: F401
    check_exit,
    ExitDecision,
    ExitAction,
    ExitReason,
)

__all__ = ["check_exit", "ExitDecision", "ExitAction", "ExitReason"]
