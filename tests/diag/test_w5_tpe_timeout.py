# -*- coding: utf-8 -*-
"""W5-3（2026-08-28 评审 P1-4）：TPE 收敛模式溢出修复钉值。

事故形态：deadline=None（--hours 0 收敛模式）下 int(float('inf')) 抛 OverflowError
被 except 吞掉——B3 战役 TPE 精修臂在收敛模式从未真正运行。
"""
import importlib.util
import sys
from pathlib import Path


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[2] / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def test_tpe_timeout_convergence_mode_finite():
    m = _load("r6_10_b3_loop_w5", "diag/r6_10_b3_loop.py")
    t = m._tpe_timeout(None)          # 收敛模式：不再 OverflowError
    assert t == 6 * 3600, "收敛模式给 6h 硬顶（进程级兜底，不悬无穷）"


def test_tpe_timeout_deadline_mode_semantics_preserved():
    import time
    m = _load("r6_10_b3_loop_w5b", "diag/r6_10_b3_loop.py")
    deadline = time.time() + 3 * 3600      # 3h 预算
    assert m._tpe_timeout(deadline) == max(600, int(3 * 3600) - 45 * 60)
    assert m._tpe_timeout(time.time() + 600) == 600   # 下限钳 600
