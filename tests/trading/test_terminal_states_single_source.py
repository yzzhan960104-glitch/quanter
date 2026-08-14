# -*- coding: utf-8 -*-
"""G7 终态集单源测试（state_store 三处 SQL IN 子句同源）。

物理意图（task-G7 Part B · 消三套集合漂移）：
    state_store 的三个查询接口对「活态/死态」的判定历史上各写一份硬编码 SQL IN 子句：
      - has_order:            state NOT IN ('REJECTED','FAILED','CANCELLED')           ← 漏 PARTIAL_CANCELLED
      - get_order_placed_qty: state NOT IN ('REJECTED','FAILED','CANCELLED','PARTIAL_CANCELLED')
      - get_pending_orders:   state IN ('PENDING','SUBMITTED','PARTIAL')
    漂移 bug：has_order 漏 PARTIAL_CANCELLED → 部分取消单被误判「已挂」→ 漏补挂
    （live 真金致命；get_order_placed_qty 的 docstring 自称「与 has_order 排除集同口径」
    却实际不一致）。本测试断言：三处同源常量、PARTIAL_CANCELLED 在 has_order 算死态。

fixture 注意：order 表 UNIQUE(account_id, trade_date, symbol, purpose)——每个 order 必须用
独立 symbol，否则同四元组冲突第二行不插入（insert_order 返 False），测试误绿。

TDD RED→GREEN。全中文注释。
"""
from __future__ import annotations

import pytest

from trading import state_store


@pytest.fixture
def db(tmp_path, monkeypatch):
    """独立 tmp db（隔离），patch _DEFAULT_DB，init_store 建 6 张表。"""
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    return db_path


def _seed_order(order_id: str, state: str, symbol: str = "600000.SH", qty: float = 100.0):
    """插 account + order（指定 state + symbol），独立 symbol 避开 UNIQUE 四元组冲突。

    UNIQUE(account_id, trade_date, symbol, purpose) 保证同四元组只有一行——多个 order
    必须用不同 symbol 才能共存（否则 insert_order 返 False 不插入）。
    """
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.insert_order(
        order_id, f"ACC1_{symbol}_2026-07-30", "ACC1", "2026-07-30", symbol,
        "buy", "OPEN", qty, 10.0, state=state)


# ============================= 单源常量一致性 =============================

def test_terminal_state_constants_disjoint_and_covering():
    """活态集与死态集不交，且覆盖除 FILLED 外的全部 DB state（FILLED 是成功终态，独立）。"""
    active = set(state_store._ACTIVE_ORDER_STATES)
    dead = set(state_store._DEAD_ORDER_STATES)
    # 不交（一个 state 不能同时活/死）
    assert active & dead == set()
    # 活态三态（PENDING/SUBMITTED/PARTIAL），PARTIAL 是 DB 短形式（OrderState.PARTIAL_FILLED）
    assert active == {"PENDING", "SUBMITTED", "PARTIAL"}
    # 死态四态（含 PARTIAL_CANCELLED——has_order 历史漏项的修复锚点）
    assert dead == {"REJECTED", "FAILED", "CANCELLED", "PARTIAL_CANCELLED"}


# ============================= has_order 修复 PARTIAL_CANCELLED 漂移 =============================

def test_has_order_treats_partial_cancelled_as_dead(db):
    """G7 修复锚点：PARTIAL_CANCELLED 是死态，has_order 应返 False（允许重挂）。

    历史 bug：has_order 的 NOT IN 漏 PARTIAL_CANCELLED → 部分取消单被误判「已挂」
    → 漏补挂（与 get_order_placed_qty 排除集不一致 = 三套集合漂移）。单源后三处同口径。
    """
    _seed_order("o1", "PARTIAL_CANCELLED")
    # PARTIAL_CANCELLED 死态 → has_order False（可重挂）
    assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "OPEN") is False


def test_has_order_all_dead_states_allow_reentry(db):
    """四种死态（REJECTED/FAILED/CANCELLED/PARTIAL_CANCELLED）has_order 均 False（可重挂）。

    每个死态用独立 symbol（UNIQUE 四元组隔离），真正测每个 state 而非只测第一个。
    """
    dead_states = ("REJECTED", "FAILED", "CANCELLED", "PARTIAL_CANCELLED")
    for i, _dead in enumerate(dead_states):
        sym = f"60000{i}.SH"   # 独立 symbol 避开 UNIQUE 冲突
        _seed_order(f"o_{_dead}", _dead, symbol=sym)
        assert state_store.has_order("ACC1", "2026-07-30", sym, "OPEN") is False, _dead


def test_has_order_filled_counts_as_placed(db):
    """FILLED（成功终态）has_order=True（已成交不重挂，但不算死态可重挂）。"""
    _seed_order("o1", "FILLED")
    assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "OPEN") is True


# ============================= get_order_placed_qty 与 has_order 同源 =============================

def test_placed_qty_excludes_all_dead_states(db):
    """get_order_placed_qty 的死态排除集与 has_order 同源（四态全排除）。

    每个死态单（独立 symbol）placed_qty=0（被 NOT IN dead 排除）；活态单 placed_qty=qty。
    """
    # 活态单：placed_qty = qty
    _seed_order("o_active", "SUBMITTED", symbol="600000.SH", qty=100.0)
    assert state_store.get_order_placed_qty(
        "ACC1", "2026-07-30", "600000.SH", "OPEN") == 100.0
    # 四种死态单（独立 symbol）：placed_qty = 0（死态排除）
    dead_states = ("REJECTED", "FAILED", "CANCELLED", "PARTIAL_CANCELLED")
    for i, _dead in enumerate(dead_states):
        sym = f"6000{i:02d}.SH"   # 600000/600001/600002/600003（与活态 600000.SH 不冲突用 600010+）
        sym = f"6000{i + 10}.SH"
        _seed_order(f"o_{_dead}", _dead, symbol=sym, qty=200.0)
        assert state_store.get_order_placed_qty(
            "ACC1", "2026-07-30", sym, "OPEN") == 0.0, _dead


# ============================= get_pending_orders 活态集一致 =============================

def test_get_pending_orders_only_active_states(db):
    """get_pending_orders 只返活态（PENDING/SUBMITTED/PARTIAL），死态与 FILLED 都排除。

    每个 state 用独立 symbol（UNIQUE 四元组隔离），get_pending_orders 按 account 查全部。
    """
    _seed_order("o_active", "SUBMITTED", symbol="600000.SH")
    _seed_order("o_partial", "PARTIAL", symbol="600001.SH")
    _seed_order("o_filled", "FILLED", symbol="600002.SH")
    _seed_order("o_pc", "PARTIAL_CANCELLED", symbol="600003.SH")
    _seed_order("o_rejected", "REJECTED", symbol="600004.SH")
    pending = state_store.get_pending_orders("ACC1")
    order_ids = {o["order_id"] for o in pending}
    assert "o_active" in order_ids
    assert "o_partial" in order_ids  # PARTIAL（活态，DB 短形式）
    assert "o_filled" not in order_ids
    assert "o_pc" not in order_ids
    assert "o_rejected" not in order_ids
