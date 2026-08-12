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
    - T1：_ACTIVE_ENGINE 单例桥已删，pre_open 改经 ports 参数；E2E 不传 ports（默认 None）
      → 跳过三段闸（与原「重置单例为 None」语义等价，无全局单例可泄漏）。
    - query_trades mock 返空（full_run 集成修复 · 根因 2）：post_close ② 归因展示段
      读 query_trades 聚合净持仓 vs position_book 产归因日志；E2E 用 tmp DB 做真相源，
      归因段聚合为空、不产误导日志、不污染 tmp position_book（W3.4 后归因段【只读展示】
      已不再重写 position_book，但隔离仍保留以避免读真实 DB 历史成交产生噪声归因）。

    A4 收口：record_live_trade（CSV 写盘）已删，conftest 不再 patch 它（patch 会
    AttributeError）。query_trades mock 保留（post_close 归因仍调它）。
    """
    from trading import job_ledger, position_book, state_store, engine

    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    # job_ledger 隔离（W5 get_ready 需 pipeline 台账 done，2026-08-05 e2e smoke 修复）：
    # 生产 pre_open gate 读 job_ledger.latest_status(pipeline)；E2E 若写真实 logs/ 台账会
    # 污染生产环境，且跨测试残留 status 会互相干扰。env 覆盖 + init_db 建表（幂等）。
    monkeypatch.setenv("TRADING_JOB_LEDGER_DB", str(tmp_path / "jobs.db"))
    job_ledger.init_db()
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("TRADE_STATE_DB", db_path)
    monkeypatch.setenv("QMT_ACCOUNT_ID", "e2e_long_acc")  # 显式 account_id 防 .env 污染

    # query_trades 隔离（full_run 集成修复 · 根因 2）：
    # 物理意图——engine.post_close ② 段读 query_trades 聚合净持仓 vs position_book
    # （tmp 空）。E2E 用 tmp DB 做真相源，mock query_trades 返空让归因段聚合为空、
    # 不产误导日志（W3.4 后归因段已降级为【只读展示】，仍隔离避免噪声日志）。
    # 范式参考 tests/trading/test_e2e_trading_flow.py:777 同款 patch。
    # A4 注：原 record_live_trade patch 已删（函数 A4 删，patch 会 AttributeError）。
    from trading import gateway_service as _svc
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
