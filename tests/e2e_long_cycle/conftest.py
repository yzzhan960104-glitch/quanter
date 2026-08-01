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

import pytest


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """隔离 position_book + state_store DB + TRADE_PLAN_DIR + review_dir 到 tmp。

    复用 tests/trading/test_e2e_trading_flow.py:isolated 范式：
    - patch position_book._DEFAULT_DB / state_store._DEFAULT_DB 到 tmp（engine 间接链路命中）。
    - TRADE_PLAN_DIR / TRADE_STATE_DB env 注入。
    - init_db / init_store 建表。
    - 重置 _ACTIVE_ENGINE 单例（防 gate 泄漏，同 test_e2e_trading_flow 范式）。
    - query_trades mock 返空（full_run 集成修复 · 根因 2）：post_close ② 兜底段
      （engine.py:1510-1548）读真实 CSV 流水聚合净持仓 vs position_book，drift 即
      reconcile_qty 改 tmp position_book 写入真实历史持仓（510300.SH 等）→ position>0
      但 fill=0（E2E 无真成交）。隔离 query_trades 返空让兜底段跳过 reconcile_qty。
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

    # query_trades 隔离（full_run 集成修复 · 根因 2）：
    # 物理意图——engine.post_close ② 段读 record_live_trade 写的 CSV 流水（logs/ 真实路径）
    # 聚合净持仓 vs position_book（tmp 空）→ drift → reconcile_qty 改 tmp position_book
    # 写入 510300.SH 等真实历史持仓 → position>0 但 fill=0（E2E 没真成交，fill 空）→
    # 表间一致性 23 日全 drift。E2E 不验证 CSV 兜底（那是 Task 11 范围），故隔离返空。
    # 注意：post_close ② 段在 engine.py:1512 是 `from presentation.server.services.trading_service
    # import query_trades as _svc_query_trades` 函数内 import，故 patch 模块级 symbol 即生效。
    # 范式参考 tests/trading/test_e2e_trading_flow.py:777 同款 patch。
    from presentation.server.services import trading_service as _svc
    monkeypatch.setattr(_svc, "query_trades", lambda *a, **k: {"trades": []})
    return tmp_path


@pytest.fixture(scope="session")
def connect_session():
    """C-7 V1：connect 5 bot 真起（session scope，整套件起/停一次）。

    物理意图（spec §4）：connect_manager.start 拉 5 个常驻 Claude Code 子进程
    （cli/trading_q/data_q/strategy_q/review）。teardown stop 树杀（taskkill /F /T）。
    session scope：避免每测试重起 5 进程（成本极高）。

    ⚠️ 真起成本：5 个 Claude Code 子进程常驻整个 E2E（~30-90min）。空转不消耗 LLM
    quota（仅 @ 响应计费）。teardown 必 stop（防进程泄漏）。
    """
    import os
    # 凭证闸：connect_manager.start 需 unified_app_id + allowed_users（.env 已配）
    from broadcast.__main__ import CONNECT_BOTS, CONNECT_DEFAULTS
    from broadcast import connect_manager

    if os.getenv("E2E_SKIP_CONNECT", "").lower() in ("1", "true"):
        yield []  # CI/无凭证环境跳过（标记 enabled=False）
        return

    started = []
    try:
        for bot in CONNECT_BOTS:
            try:
                connect_manager.start(bot, CONNECT_BOTS[bot], CONNECT_DEFAULTS)
                started.append(bot)
            except RuntimeError:
                pass  # 配置缺失跳过（C-7 V1 软降级）
        yield started
    finally:
        for bot in started:
            try:
                connect_manager.stop(bot)
            except Exception:
                pass
