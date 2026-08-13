# -*- coding: utf-8 -*-
"""pre_open 基线 T-1 close 兜底回填单测（DG-G3 · 2026-08-13）。

物理定位（spec G3 · audit-remediation-design §G3 · 红线「fail-closed + 基线 T-1 兜底同 commit」）：
    breaker fail-closed 改动后，若 pre_open 抓精确基线失败（query_asset 返空 / 异常 / gw=None）
    且无兜底，则 post_close 读 start 仍为 None → breaker fail-closed 触发停手 →
    **每天开盘误熔断中断业务**（即使账户健康）。本测试守护的兜底链路是：
        pre_open 抓精确基线失败 → 用 T-1 close 写入 account_daily.start（近似基线）
    → post_close 读 start 非 None → breaker 正常判定（fail-closed 仅在 T-1 close 也无时触发）。

    Why pre_open 写而不让 post_close 本地兜底：
        post_close 已有本地 T-1 兜底（``get_prev_close_equity`` 读 T-1 close 作 start 近似），
        但只在 post_close 内存中——不写回 DB → 其他读口（如运维查询/cockpit 观测）
        仍读到 start=None。pre_open 在抓失败时**主动写 T-1 close 到 account_daily.start**
        = 让 DB 真相源持有近似基线，所有读口统一受益（SSoT 原则）。

测试边界：
    三种 pre_open 抓精确基线失败场景（query_asset 返空 / 异常 / gw=None），均验证
    T-1 close 被回填到 account_daily.start；并守护「无 T-1 close 时不写脏值」边界
    （保留 None 让 breaker fail-closed，绝不拿 0/None 写基线）。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from trading import clock, engine, state_store


# ============================================================================
# 公共 fixture：隔离 state_store DB + 默认 dry_run（防真单 + 防测试真发钉钉）
# ============================================================================
@pytest.fixture
def _state_db(tmp_path, monkeypatch):
    """独立 state_store DB（与 test_engine.py 同名 fixture 同语义，本文件自包含）。"""
    db_path = str(tmp_path / "baseline.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    return db_path


def _seed_t1_close(account_id: str, t1_date: str, close_total: float) -> None:
    """种 T-1 行的 close_total_asset（模拟前一日 post_close 已写收盘快照）。"""
    if state_store.get_account(account_id) is None:
        state_store.upsert_account(account_id, broker="qmt")
    state_store.snapshot_close_equity(account_id, t1_date, close_total)


def _seed_one_signal(account_id: str, plan_date: str) -> None:
    """种当日 DB SIGNAL + CONFIRMED（pre_open 确认闸前置，参考 test_engine._seed_signals_db）。

    极简单标的——pre_open 挂单段在本测试中应被 monkeypatch 拦截（_submit 不被调），
    仅需让 pre_open 走到「抓基线」段（不返「无计划」）。
    """
    if state_store.get_account(account_id) is None:
        state_store.upsert_account(account_id, broker="qmt")
    sym = "300001.SZ"
    tid = state_store.build_trade_id(account_id, sym, plan_date)
    meta_obj = {
        "order": {"symbol": sym, "qty": 100, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
        "formed_at": plan_date, "max_wait": 5,
        "plan_date": plan_date, "strategy_name": "neckline",
        "rationale": "DG-G3 baseline backfill test",
    }
    # SIGNAL 先写、CONFIRMED 后写（event_id 顺序保证 get_latest_action 返 CONFIRMED）
    state_store.insert_trade_event(
        account_id, tid, sym, "SIGNAL",
        meta=json.dumps(meta_obj, ensure_ascii=False))
    state_store.insert_trade_event(account_id, tid, sym, "CONFIRMED")


def _patch_pre_open_to_baseline_stage(monkeypatch, fake_gw, *, mode="dry_run") -> list:
    """拦截 pre_open 挂单段副作用，让测试只验「抓基线」段。

    - get_gateway → fake_gw（控制 query_asset 行为）
    - _cancel_all_open_orders → no-op（不真撤单）
    - _submit → 抛 AssertionError（本测不应触达挂单）
    - _mode → 固定 dry_run（避免 live 触发真钉钉副用；live 由 test_engine 覆盖）
    - _alert_critical → 拦截收集（验告警语义但不真发）
    """
    monkeypatch.setattr("trading.phases.pre_open.get_gateway", lambda: fake_gw)
    async def _no_cancel(gw, *a, **kw):
        return {"cancelled": 0, "unconfirmed": 0}
    monkeypatch.setattr("trading.phases.pre_open._cancel_all_open_orders", _no_cancel)
    async def _no_submit(order, **kw):
        raise AssertionError("baseline 兜底测不应触达挂单")
    monkeypatch.setattr("trading.phases.pre_open._submit", _no_submit)
    monkeypatch.setattr("trading.phases.pre_open._mode", lambda: mode)
    alerts: list[str] = []
    monkeypatch.setattr(
        "trading.phases.pre_open._alert_critical", lambda msg: alerts.append(msg))
    return alerts


class _EmptyAssetGw:
    """query_asset 返空 dict（模拟未连接/锁定/超时）。"""
    async def query_asset(self):
        return {}


class _RaisingAssetGw:
    """query_asset 抛 RuntimeError（模拟超时/锁定报错）。"""
    async def query_asset(self):
        raise RuntimeError("query_stock_asset 超时")


# ============================================================================
# 场景 1：query_asset 返空 + T-1 close 有值 → 回填 account_daily.start
# ============================================================================

def test_pre_open_backfills_start_from_t1_close_when_query_asset_empty(monkeypatch, _state_db):
    """pre_open query_asset 返空 + T-1 close 有值 → 用 T-1 close 回填 account_daily.start。

    物理意图（DG-G3 红线「fail-closed + 基线兜底同 commit」）：
        精确抓基线失败时，T-1 close 作「隔夜无交易近似」写入 account_daily.start，
        让 post_close 读到非 None 基线 → 熔断正常工作，避免每天开盘误熔断中断业务。
    """
    today = "2099-01-02"
    t1 = "2099-01-01"
    aid = engine._resolve_account_id()
    # mock T-1 = 2099-01-01（避免日历依赖），种 T-1 close = 1_234_567
    monkeypatch.setattr(clock, "pretrade_date", lambda d: t1)
    _seed_t1_close(aid, t1, 1_234_567.0)
    _seed_one_signal(aid, today)

    alerts = _patch_pre_open_to_baseline_stage(monkeypatch, _EmptyAssetGw(), mode="live")

    asyncio.run(engine.pre_open(today))

    # 断言：T-1 close 回填到当日 account_daily.start（DB 真相源持近似基线）
    backfilled = state_store.get_start_equity(aid, today)
    assert backfilled == 1_234_567.0, (
        f"query_asset 返空 + T-1 close 有值时，account_daily.start 应回填为 T-1 close"
        f"（实际: {backfilled}）")
    # 告警仍推（live 模式 _alert_critical 被调，让运维知情精度边界）
    assert any("query_asset 返空" in m or "熔断基线缺失" in m for m in alerts), (
        "回填 T-1 close 后仍应推 CRITICAL 让运维知情精度边界")


# ============================================================================
# 场景 2：query_asset 抛异常 + T-1 close 有值 → 回填 account_daily.start
# ============================================================================

def test_pre_open_backfills_start_from_t1_close_on_query_asset_exception(monkeypatch, _state_db):
    """pre_open query_asset 抛异常 + T-1 close 有值 → 同样回填 T-1 close（异常分支无差别）。"""
    today = "2099-01-02"
    t1 = "2099-01-01"
    aid = engine._resolve_account_id()
    monkeypatch.setattr(clock, "pretrade_date", lambda d: t1)
    _seed_t1_close(aid, t1, 999_999.0)
    _seed_one_signal(aid, today)

    alerts = _patch_pre_open_to_baseline_stage(monkeypatch, _RaisingAssetGw(), mode="live")

    asyncio.run(engine.pre_open(today))

    backfilled = state_store.get_start_equity(aid, today)
    assert backfilled == 999_999.0, (
        f"query_asset 异常分支也应回填 T-1 close（实际: {backfilled}）")
    assert any("抓取异常" in m or "熔断基线缺失" in m for m in alerts)


# ============================================================================
# 场景 3：gw=None（网关未装配） + T-1 close 有值 → 回填 account_daily.start
# ============================================================================

def test_pre_open_backfills_start_from_t1_close_when_gw_none(monkeypatch, _state_db):
    """pre_open gw=None（网关未装配） + T-1 close 有值 → 同样回填 T-1 close。

    场景：catchup 补跑 / 启动早期 gw 未连上 → pre_open 仍能基于 T-1 close 写近似基线。
    """
    today = "2099-01-02"
    t1 = "2099-01-01"
    aid = engine._resolve_account_id()
    monkeypatch.setattr(clock, "pretrade_date", lambda d: t1)
    _seed_t1_close(aid, t1, 888_888.0)
    _seed_one_signal(aid, today)

    alerts = _patch_pre_open_to_baseline_stage(monkeypatch, None, mode="live")  # gw=None

    asyncio.run(engine.pre_open(today))

    backfilled = state_store.get_start_equity(aid, today)
    assert backfilled == 888_888.0, (
        f"gw=None 分支也应回填 T-1 close（实际: {backfilled}）")
    assert any("gw=None" in m or "熔断基线缺失" in m for m in alerts)


# ============================================================================
# 边界：T-1 close 也无 → 不写脏值（保留 None 让 breaker fail-closed）
# ============================================================================

def test_pre_open_no_backfill_when_t1_close_also_missing(monkeypatch, _state_db):
    """pre_open query_asset 返空 + T-1 close 也无 → 不写脏值（保留 None 让 breaker fail-closed）。

    物理意图（DG-G3 防御性深度）：
        基线链全失效时绝不拿 0/None 写 account_daily.start（否则 post_close 读到 0
        会触发除零或语义模糊判定）。让 None 保留 → post_close 直传 breaker →
        fail-closed 分支处理（dry 停手 / live halt）。
    """
    today = "2099-01-02"
    t1 = "2099-01-01"
    aid = engine._resolve_account_id()
    monkeypatch.setattr(clock, "pretrade_date", lambda d: t1)
    # 不种 T-1 close（基线链全失效）
    _seed_one_signal(aid, today)

    alerts = _patch_pre_open_to_baseline_stage(monkeypatch, _EmptyAssetGw(), mode="live")

    asyncio.run(engine.pre_open(today))

    # 断言：account_daily.start 仍为 None（未写脏值），让 breaker fail-closed 处理
    assert state_store.get_start_equity(aid, today) is None, (
        "T-1 close 也无时不应写脏值，应保留 None 让 breaker fail-closed")
    # live 模式仍推 CRITICAL 告警（让运维知情基线链全失效，breaker 将 fail-closed 停手）
    assert len(alerts) >= 1, (
        "T-1 close 也无时仍应推 CRITICAL 让运维知情基线链全失效")
