# -*- coding: utf-8 -*-
"""T14 断潜伏 import 环：两种加载序（broker-first / trading-first）都必须独立可 import。

Why 子进程级（T13 披露 · pre-existing 潜伏环）：
    broker/__init__ → broker.base:44（from trading.compute.types import ...）
    → trading/__init__（from .order_state import ...）→ order_state:71
    （from trading import gateway_service）→ gateway_service:36
    （from broker.base import OrderResult → broker.base 半初始化）→ ImportError。

    环只在「进程首次模块加载序」暴露：pytest 进程早已 import 过 trading/broker，
    进程内再 import 永远绿——必须每个用例起净子进程验证（结果不依赖任何执行顺序，
    两条加载序互不污染）。CI/开发机无 xtquant 时 broker.qmt 自身容错加载，同样适用。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _import_in_fresh_process(stmt: str) -> subprocess.CompletedProcess:
    """净子进程执行 import 语句（UTF-8 + 不设 QUANTER_TESTING，与生产口径一致）。"""
    env = {**os.environ, "PYTHONUTF8": "1"}
    # 全量约束：验证不设测试旗标——加载序必须在生产语义下成立
    env.pop("QUANTER_TESTING", None)
    return subprocess.run(
        [sys.executable, "-c", stmt],
        capture_output=True, text=True, errors="replace",
        cwd=str(ROOT), env=env, timeout=180)


def test_import_broker_qmt_first_does_not_crash():
    """broker-first 加载序：`import broker.qmt` 必须不炸（断 gateway_service 环的回归哨）。"""
    r = _import_in_fresh_process("import broker.qmt")
    assert r.returncode == 0, (
        f"broker-first 加载序崩溃（潜伏 import 环回归）：\n{r.stderr}")


def test_import_trading_engine_first_does_not_crash():
    """trading-first 加载序：`import trading.engine` 必须不炸（保既有主链不被断环改动误伤）。"""
    r = _import_in_fresh_process("import trading.engine")
    assert r.returncode == 0, (
        f"trading-first 加载序崩溃（断环改动误伤主链）：\n{r.stderr}")
