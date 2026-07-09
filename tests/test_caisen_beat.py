# -*- coding: utf-8 -*-
"""Celery beat 三任务测试（蔡森形态学流水线 Phase 3 · Task 5）。

物理意图与覆盖节点（CLAUDE.md 量化风控·边界审查）：
  本测试验证蔡森形态学流水线 Phase 3 的"自动调度层"——在 server/celery_app.py
  挂载三个 @celery_app.task：
    1. caisen.scan_universe       —— T 日 15:30 调 caisen_service.run_scan 跑全市场扫描；
    2. caisen.monitor_pullback    —— 盘中每 60s 跑 ExecutionEngine.tick_pullback（ARMED→FILLED）；
    3. caisen.monitor_holding     —— 盘中每 60s 跑 ExecutionEngine.tick_exit（FILLED→CLOSED）。

  监控任务的两道跳过闸门（断线保护 + 交易时段挡板）：
    - 非交易时段（_in_a_share_session=False）→ 直接 return，不查行情/不下单
      （隔夜/周末 beat 空转，避免无意义计算与误下单）；
    - trading_service.get_status().mode != "live" → return
      （网关 unavailable/disconnected/vetoed_by_risk 时断线不补发，等下一轮重连）。

  async 包裹：tick_pullback / tick_exit 是 async 方法，Celery 同步任务内用
  asyncio.run() 包裹驱动事件循环（Celery worker 默认 prefork 同步执行模型）。

设计要点（CLAUDE.md 极简 + 显式原则）：
  - mock caisen_service.run_scan / trading_service._in_a_share_session / get_status /
    ExecutionEngine 构造 + tick_*（AsyncMock），完全隔离 I/O 与状态机；
  - 不依赖真 Redis / Celery worker（@celery_app.task 装饰器在 import 期注册，测试
    直接调被装饰函数的 .__wrapped__ 或直接调函数体即可——本测试调原函数对象）；
  - beat_schedule 配置断言：三任务名 + Asia/Shanghai 时区。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from server import celery_app as celery_app_mod
from server.services import trading_service


# ---------------------------------------------------------------------------
# scan_universe：调 caisen_service.run_scan
# ---------------------------------------------------------------------------
def test_scan_universe_calls_run_scan(monkeypatch):
    """scan_universe 调 caisen_service.run_scan（全市场 universe + 当日 date）。

    物理意图：T 日收盘扫描 beat 触发后，scan_universe 应委托 caisen_service.run_scan
    完成扫描→生成→落盘全链路。此处只验证"委托关系"（run_scan 被调用一次），
    不验证扫描算法（已在 test_caisen_service.py 覆盖）。

    follow-up：全市场 universe 装配当前占位（data_lake 未接），run_scan 收到空
    universe 时按契约返回空列表——这是已知降级，待 Phase 3+ 接 data_lake 后生效。
    """
    called = {}

    def fake_run_scan(req):
        # 记录调用入参，返回空列表（不进入扫描算法链路，隔离 I/O）
        called["req"] = req
        return []

    # mock caisen_service.run_scan：屏蔽 screener/plan/storage 真实链路
    monkeypatch.setattr(
        "server.celery_app.caisen_service.run_scan", fake_run_scan
    )

    celery_app_mod.scan_universe()

    # 断言：run_scan 被调用，且传入的是 ScanRequest 实例（date 非空、universe 为列表）
    assert "req" in called, "scan_universe 未调用 caisen_service.run_scan"
    req = called["req"]
    # ScanRequest 契约：date=str / universe=list / cfg_override=dict
    assert isinstance(req.date, str) and req.date
    assert isinstance(req.universe, list)


# ---------------------------------------------------------------------------
# monitor_pullback：非交易时段 / 非 live 跳过；交易时段 + live 调 tick_pullback
# ---------------------------------------------------------------------------
def test_monitor_pullback_skips_off_session(monkeypatch):
    """非交易时段（_in_a_share_session=False）→ 不调 tick_pullback（隔夜空转保护）。

    物理意图：A 股非交易时段（隔夜/周末/午休）行情不更新、挂单无意义，beat 触发
    时应直接 return，不进入 tick_pullback 编排链路。
    """
    # 非交易时段
    monkeypatch.setattr(trading_service, "_in_a_share_session", lambda: False)
    # 即便误入编排，tick_pullback 也应是 mock（断言未被调用）
    fake_engine = MagicMock()
    fake_engine.tick_pullback = AsyncMock()
    monkeypatch.setattr(
        "server.celery_app._build_execution_engine", lambda: fake_engine
    )

    celery_app_mod.monitor_pullback()

    fake_engine.tick_pullback.assert_not_called()


def test_monitor_pullback_skips_not_live(monkeypatch):
    """交易时段但 trading_service 非 live（unavailable/disconnected/locked）→ 跳过。

    物理意图：断线不补发——网关 unavailable/disconnected/vetoed_by_risk 时行情/下单
    均不可靠，beat 本轮跳过，等下一轮重连后再处理。
    """
    monkeypatch.setattr(trading_service, "_in_a_share_session", lambda: True)
    # 网关未连接（disconnected）
    monkeypatch.setattr(
        "server.celery_app.trading_service.get_status",
        lambda: {"connected": False, "locked": False, "mode": "disconnected"},
    )
    fake_engine = MagicMock()
    fake_engine.tick_pullback = AsyncMock()
    monkeypatch.setattr(
        "server.celery_app._build_execution_engine", lambda: fake_engine
    )

    celery_app_mod.monitor_pullback()

    fake_engine.tick_pullback.assert_not_called()


def test_monitor_pullback_runs_in_session(monkeypatch):
    """交易时段 + live → 调 ExecutionEngine.tick_pullback（async 用 asyncio.run 包裹）。

    物理意图：盘中每 60s beat 驱动 ARMED→FILLED 状态机推进——挂单触及回踩区间即
    限价买入。tick_pullback 是 async 方法，Celery 同步任务用 asyncio.run 包裹。
    """
    monkeypatch.setattr(trading_service, "_in_a_share_session", lambda: True)
    monkeypatch.setattr(
        "server.celery_app.trading_service.get_status",
        lambda: {"connected": True, "locked": False, "mode": "live"},
    )
    fake_engine = MagicMock()
    fake_engine.tick_pullback = AsyncMock()
    monkeypatch.setattr(
        "server.celery_app._build_execution_engine", lambda: fake_engine
    )

    celery_app_mod.monitor_pullback()

    fake_engine.tick_pullback.assert_awaited_once()


# ---------------------------------------------------------------------------
# monitor_holding：调 tick_exit
# ---------------------------------------------------------------------------
def test_monitor_holding_skips_off_session(monkeypatch):
    """非交易时段 → 不调 tick_exit（同 monitor_pullback 隔夜保护语义）。"""
    monkeypatch.setattr(trading_service, "_in_a_share_session", lambda: False)
    fake_engine = MagicMock()
    fake_engine.tick_exit = AsyncMock()
    monkeypatch.setattr(
        "server.celery_app._build_execution_engine", lambda: fake_engine
    )

    celery_app_mod.monitor_holding()

    fake_engine.tick_exit.assert_not_called()


def test_monitor_holding_calls_tick_exit(monkeypatch):
    """交易时段 + live → 调 ExecutionEngine.tick_exit（FILLED→CLOSED 离场编排）。

    物理意图：盘中每 60s beat 遍历 FILLED 持仓，check_exit 命中止损/止盈/时间止损
    即市价平仓，并推进移动止盈止损上移。
    """
    monkeypatch.setattr(trading_service, "_in_a_share_session", lambda: True)
    monkeypatch.setattr(
        "server.celery_app.trading_service.get_status",
        lambda: {"connected": True, "locked": False, "mode": "live"},
    )
    fake_engine = MagicMock()
    fake_engine.tick_exit = AsyncMock()
    monkeypatch.setattr(
        "server.celery_app._build_execution_engine", lambda: fake_engine
    )

    celery_app_mod.monitor_holding()

    fake_engine.tick_exit.assert_awaited_once()


# ---------------------------------------------------------------------------
# beat_schedule 配置：三任务 + Asia/Shanghai 时区
# ---------------------------------------------------------------------------
def test_beat_schedule_configured():
    """celery_app.conf.beat_schedule 含三任务 + timezone=Asia/Shanghai。

    物理意图：
        - caisen-scan-daily       crontab(15:30)  T 日收盘扫描（A 股 15:00 收盘，
                                  15:30 留 30min 缓冲等收盘数据落盘）；
        - caisen-monitor-pullback 60.0s           盘中回踩监控（任务内判交易时段）；
        - caisen-monitor-holding  60.0s           盘中持仓离场监控。
        - timezone=Asia/Shanghai  crontab 按东八区触发（A 股交易日历对齐）。
    """
    sched = celery_app_mod.celery_app.conf.beat_schedule
    assert "caisen-scan-daily" in sched
    assert sched["caisen-scan-daily"]["task"] == "caisen.scan_universe"
    assert "caisen-monitor-pullback" in sched
    assert sched["caisen-monitor-pullback"]["task"] == "caisen.monitor_pullback"
    assert "caisen-monitor-holding" in sched
    assert sched["caisen-monitor-holding"]["task"] == "caisen.monitor_holding"

    assert celery_app_mod.celery_app.conf.timezone == "Asia/Shanghai"
