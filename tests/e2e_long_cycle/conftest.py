# -*- coding: utf-8 -*-
"""C1-C7 长周期 E2E 共享 fixture（spec §3 编排层）。

物理意图：23 日时序回放的共享隔离层——
- tmp DB（position_book + state_store）+ TRADE_PLAN_DIR/review_dir：不污染真实 logs/。
- clock freeze hook：ReplayDriver 每阶段 patch trading.clock.now（C-6 单一口子）。
- connect session 起/停（V5 补）：5 bot 真起 + teardown 树杀。
- 钉钉推送日志（V5 补）：patch fire_and_forget 真推 + 落表。

Why conftest 而非每个测试重复：23 日回放是多测试共享的重装配（DB/clock/connect），
conftest session/module scope 复用避免每测试重起 connect 5 进程（成本极高）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """隔离 position_book + state_store DB + TRADE_PLAN_DIR + review_dir 到 tmp。

    复用 tests/trading/test_e2e_trading_flow.py:isolated 范式：
    - patch position_book._DEFAULT_DB / state_store._DEFAULT_DB 到 tmp（engine 间接链路命中）。
    - TRADE_PLAN_DIR / TRADE_STATE_DB env 注入。
    - init_db / init_store 建表。
    - 重置 _ACTIVE_ENGINE 单例（防 gate 泄漏，同 test_e2e_trading_flow 范式）。
    """
    from trading import position_book, state_store, engine

    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("TRADE_STATE_DB", db_path)
    monkeypatch.setenv("QMT_ACCOUNT_ID", "e2e_long_acc")  # 显式 account_id 防 .env 污染
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", None)  # 防 gate 泄漏
    return tmp_path
