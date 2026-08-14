# -*- coding: utf-8 -*-
"""G7 FSM 写校验测试（state_store.update_order_state 过 OrderStateMachine 校验）。

物理意图（task-G7 Part B · FSM 收口）：
    update_order_state 原裸 UPDATE order.state，不过 OrderStateMachine 校验——非法迁移
    （如 FILLED→PENDING 倒退、终态后再跳变）静默落库，状态机约束形同虚设。本测试断言：
    update_order_state 写入前读 old_state，过 _is_valid_transition 校验。

实现模式（软告警 · brief 边界授权）：
    非法迁移 → logger.warning（含 old/new/order_id）+ **仍写入**（不拒写），幂等回推
    （old==new）静默。Why 软告警而非硬拒：FSM 迁移表归 architecture/05 红线不可改
    （order_state.py 顶部注释明示），而生产 pre_open 存在 PENDING→REJECTED 等表外业务
    跳变、test_has_order_filters_dead_states 存在终态循环——硬拒会破生产 + 测试，白名单
    方案需改 FSM 表 → 越红线。软告警达成「消监控盲区」顶层目标（非法迁移不再静默），
    硬拒写延后至 FSM 表重构 Task（见 G7 报告 concerns）。

TDD RED→GREEN。全中文注释。
"""
from __future__ import annotations

import logging

import pytest

from trading import state_store


@pytest.fixture
def db(tmp_path, monkeypatch):
    """独立 tmp db（隔离），patch _DEFAULT_DB，init_store 建 6 张表。"""
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    return db_path


def _seed(order_id: str, state: str, symbol: str = "600000.SH"):
    """插 account + order（指定初始 state + symbol），独立 symbol 避开 UNIQUE 四元组冲突。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.insert_order(
        order_id, f"ACC1_{symbol}_2026-07-30", "ACC1", "2026-07-30", symbol,
        "buy", "OPEN", 100, 10.0, state=state)


def _get_state(order_id: str, db: str) -> str:
    """直读 DB order.state（绕过 update_order_state，拿真相源）。"""
    import sqlite3
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        row = con.execute('SELECT state FROM "order" WHERE order_id=?', (order_id,)).fetchone()
    return row["state"] if row else None


# ============================= 非法迁移：告警（软告警仍写） =============================

def test_illegal_transition_warned(db, caplog):
    """FILLED→PENDING 终态倒退（FSM 非法）→ caplog warning + 状态被记录。

    软告警模式：非法迁移告警让人看见（消盲区），但仍写入（不破现有测试/生产）。
    硬拒写需 FSM 表重构（architecture/05 红线），见 G7 报告 concerns。
    """
    import sqlite3
    _seed("o1", "FILLED")
    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        # FILLED→PENDING：终态倒退，FSM 表 FILLED:[] 非法
        state_store.update_order_state("o1", "PENDING")
    # 可观测：caplog 有 warning 含迁移信息（偏离 FSM 表）
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("FILLED" in m and "PENDING" in m and "o1" in m for m in msgs), \
        f"期望 FILLED→PENDING 非法迁移告警，实得：{msgs}"
    # 软告警仍写入（不拒写，不破测试/生产）
    assert _get_state("o1", db) == "PENDING"


# ============================= 合法迁移：无告警 =============================

def test_legal_transition_no_warning(db, caplog):
    """SUBMITTED→FILLED（合法迁移）→ 无 warning + 行更新。"""
    _seed("o1", "SUBMITTED")
    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        state_store.update_order_state("o1", "FILLED", filled_qty=100, filled_price=10.5)
    # 合法迁移无告警
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    # 允许其他无关 warning，但不应有「偏离 FSM」的告警
    assert not any("偏离" in m or "非法" in m for m in msgs), f"合法迁移不应告警，实得：{msgs}"
    # 行更新
    assert _get_state("o1", db) == "FILLED"


# ============================= broker 异步回报合法路径：不误拒 =============================

def test_broker_callback_legal_path(db, caplog):
    """broker 部分成交序列 SUBMITTED→PARTIAL→FILLED 全程合法 → 无告警 + 推进正确。

    边界（brief 关键）：FSM 校验不能拒合法 broker 异步回报序列。本测试覆盖部分成交的
    合法跳变链，确保校验不误拒真实 broker 回报。
    """
    _seed("o1", "SUBMITTED")
    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        # SUBMITTED → PARTIAL（部分成交，DB 短形式 = OrderState.PARTIAL_FILLED）
        state_store.update_order_state("o1", "PARTIAL", filled_qty=30)
        assert _get_state("o1", db) == "PARTIAL"
        # PARTIAL → FILLED（累计成交满）
        state_store.update_order_state("o1", "FILLED", filled_qty=100)
        assert _get_state("o1", db) == "FILLED"
    # 全程合法迁移，无「偏离 FSM」告警
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("偏离" in m or "非法" in m for m in msgs), \
        f"broker 合法回报路径不应告警，实得：{msgs}"


def test_broker_cancel_and_reject_legal_path(db, caplog):
    """broker 撤单/拒单合法路径 SUBMITTED→CANCELLED / SUBMITTED→REJECTED → 无告警。"""
    _seed("o1", "SUBMITTED", symbol="600000.SH")
    _seed("o2", "SUBMITTED", symbol="600001.SH")  # 独立 symbol 避开 UNIQUE 冲突
    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        state_store.update_order_state("o1", "CANCELLED")
        state_store.update_order_state("o2", "REJECTED")
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("偏离" in m or "非法" in m for m in msgs), msgs
    assert _get_state("o1", db) == "CANCELLED"
    assert _get_state("o2", db) == "REJECTED"


# ============================= 幂等回推：静默 =============================

def test_idempotent_replay_silent(db, caplog):
    """FILLED→FILLED 幂等回推（broker 重推同终态）→ 无告警 + no-op。

    边界：broker 在部分成交/柜台重推时会重放同一终态，FSM 表 FILLED:[] 会判它「非法」，
    但幂等回推是合法 no-op，校验须 short-circuit 静默（old==new 不告警）。
    """
    _seed("o1", "FILLED")
    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        state_store.update_order_state("o1", "FILLED", filled_qty=100)
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("偏离" in m or "非法" in m for m in msgs), \
        f"幂等回推不应告警，实得：{msgs}"
    assert _get_state("o1", db) == "FILLED"


# ============================= 软告警不破 pre_open 生产路径 =============================

def test_pre_open_pending_to_rejected_warned_but_written(db, caplog):
    """pre_open PENDING→REJECTED（FSM 表外业务跳变）→ 告警 + 仍写入（软告警不破生产）。

    边界（brief 关键）：pre_open 挂单被拒时回填 PENDING→REJECTED（trading/phases/pre_open.py:525），
    FSM 表 PENDING:[SUBMITTED,FAILED] 不含 REJECTED，属表外业务跳变。软告警模式：告警让
    运维看见（可观测），但仍写入（不破 pre_open 死单回填生产路径）。硬拒写会破此路径。
    """
    _seed("o1", "PENDING")
    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        # pre_open 失败回填 REJECTED（PENDING→REJECTED 表外跳变）
        state_store.update_order_state("o1", "REJECTED")
    # 告警可见（偏离 FSM 表，运维可见 pre_open 在做表外跳变）
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("PENDING" in m and "REJECTED" in m for m in msgs), msgs
    # 软告警仍写入（pre_open 生产路径不破）
    assert _get_state("o1", db) == "REJECTED"
