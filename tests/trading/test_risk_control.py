# -*- coding: utf-8 -*-
"""risk_control 人工风控双值单测（ADR-16 · 2026-08-17）。

覆盖契约：
1. 空表（缺键）→ resolve 默认 block=False / max_pos=1.0（零行为变化起步）；
2. 写读往返：block on/off、position 数值 → resolve 反映新值（UPSERT 幂等可覆盖）；
3. 值域校验 fail-closed：position 越界 / 空写 → ValueError 拒写（不静默钳制）；
4. DB 异常 → resolve fail-closed 全拦（block=True / max_pos=0.0 / degraded=True）；
5. raw 读坏值（直写非法字符串）→ resolve 按默认 + degraded=True（观测可辨）；
6. CLI 冒烟：block on / position 0.8 / 查看（独立进程语义，单进程内直调 _main）；
7. check_order master_switch 关：只拦真买单，卖出/dry_run 不拦，缺省零行为变化；
8. pre_open 消费点：block 全拦（skip payload）/ 总仓位 fail-closed（无权益）/ 逐单额度跳过。
"""
from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading import state_store


@pytest.fixture
def risk_db(tmp_path, monkeypatch):
    """隔离的 trading_state.db（risk_control 表随 init_store 建）。"""
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    state_store.init_store()
    return db


def test_empty_table_resolves_defaults(risk_db):
    """空表缺键 → block=False / max_pos=1.0 / degraded=False（ADR-16 零行为变化起步）。"""
    r = state_store.resolve_risk_control()
    assert r == {"block": False, "max_pos": 1.0, "degraded": False}


def test_write_read_roundtrip_block(risk_db):
    """block on → 拦；再 off → 放行（UPSERT 覆盖）。"""
    r1 = state_store.write_risk_control(block_new_orders=True)
    assert r1["block"] is True and r1["degraded"] is False
    assert state_store.resolve_risk_control()["block"] is True
    r2 = state_store.write_risk_control(block_new_orders=False)
    assert r2["block"] is False
    raw = state_store.read_risk_control()
    assert raw["block_new_orders"] == "0"


def test_write_read_roundtrip_position(risk_db):
    """position 0.8 → resolve 0.8；raw 存数值字符串。"""
    state_store.write_risk_control(max_total_position=0.8)
    r = state_store.resolve_risk_control()
    assert r["max_pos"] == pytest.approx(0.8)
    assert state_store.read_risk_control()["max_total_position"] == "0.8000"


def test_write_position_out_of_range_rejected(risk_db):
    """position 越界（1.5 / -0.1）→ ValueError 拒写（人工风控意志设错必须显式失败）。"""
    with pytest.raises(ValueError, match="越界"):
        state_store.write_risk_control(max_total_position=1.5)
    with pytest.raises(ValueError, match="越界"):
        state_store.write_risk_control(max_total_position=-0.1)
    # 拒写后表仍空（缺键默认）
    assert state_store.resolve_risk_control()["max_pos"] == 1.0


def test_write_no_keys_rejected(risk_db):
    """两键全 None → 空写拒收。"""
    with pytest.raises(ValueError, match="至少传一个键"):
        state_store.write_risk_control()


def test_resolve_fail_closed_on_db_error(tmp_path):
    """DB 无法打开（路径是目录——建表自愈也救不了）→ fail-closed 全拦 + degraded。

    注：普通「文件不存在」路径已被 _ensure_risk_control_table 自愈（首访建库建表，
    resolve 返缺省不拦）——只有真 IO 级失败（目录当文件/权限/损坏）才触发本分支。
    """
    bad = str(tmp_path / "as_dir")   # 目录当 DB 文件 → sqlite3.OperationalError
    (tmp_path / "as_dir").mkdir()
    r = state_store.resolve_risk_control(db_path=bad)
    assert r == {"block": True, "max_pos": 0.0, "degraded": True}


def test_resolve_degrades_on_corrupt_value(risk_db):
    """raw 坏值（绕过 write 直写非法字符串）→ 按默认执行 + degraded=True。"""
    with sqlite3.connect(risk_db) as con:
        con.execute("INSERT INTO risk_control(key, value, updated_at) VALUES(?, ?, ?)",
                    ("block_new_orders", "yes", "2026-08-17T00:00:00"))
        con.execute("INSERT INTO risk_control(key, value, updated_at) VALUES(?, ?, ?)",
                    ("max_total_position", "abc", "2026-08-17T00:00:00"))
    # review I-4：值损坏 fail-closed——block 视同拦截、max_pos 视同 0（非旧默认放开）
    r = state_store.resolve_risk_control()
    assert r["block"] is True and r["max_pos"] == 0.0 and r["degraded"] is True


def test_cli_block_and_position(risk_db, capsys):
    """CLI 冒烟：block on → position 0.8 → 查看输出（直调 _main，同独立进程语义）。"""
    from trading import risk_ctrl
    assert risk_ctrl._main(["block", "on"]) == 0
    assert state_store.resolve_risk_control()["block"] is True
    assert risk_ctrl._main(["position", "0.8"]) == 0
    r = state_store.resolve_risk_control()
    assert r["block"] is True and r["max_pos"] == pytest.approx(0.8)
    assert risk_ctrl._main([]) == 0
    out = capsys.readouterr().out
    assert "拦截增量下单" in out and "80%" in out


def test_cli_rejects_bad_args(risk_db, capsys):
    """CLI 非法参数 → 退出码 2 + stderr 提示（不落库）。"""
    from trading import risk_ctrl
    assert risk_ctrl._main(["block", "maybe"]) == 2
    assert risk_ctrl._main(["position", "1.5"]) == 2
    assert state_store.resolve_risk_control()["block"] is False


# ============================================================================
# check_order master_switch 关（ADR-16：只拦真买单，卖出/模拟不拦）
# ============================================================================
def _order(side="buy"):
    from trading.compute.types import OrderRequest
    return OrderRequest(symbol="300214.SZ", qty=100, side=side, price=10.0)


def _ok(**kw):
    base = dict(dry_run=False, enforce_session=True, is_locked=False,
                connected=True, in_session=True)
    base.update(kw)
    return base


def test_master_switch_blocks_buy():
    from trading.compute.risk import check_order
    d = check_order(_order("buy"), **_ok(block_new_orders=True))
    assert d.blocked and d.stage == "master_switch" and not d.is_dry_run


def test_master_switch_never_blocks_sell():
    """卖出（止损/止盈/超期退出）永不拦——开关只拦增量买入。"""
    from trading.compute.risk import check_order
    d = check_order(_order("sell"), **_ok(block_new_orders=True))
    assert not d.blocked


def test_master_switch_spared_for_dry_run():
    """dry_run 模拟不产生真增量 → 不受开关限制（闸序：dry_run 在 master_switch 前）。"""
    from trading.compute.risk import check_order
    d = check_order(_order("buy"), **_ok(dry_run=True, block_new_orders=True))
    assert d.blocked and d.is_dry_run and d.stage == "dry_run"


def test_master_switch_default_off_zero_behavior():
    """缺省 block_new_orders=False → 零行为变化（老调用方不传即原语义）。"""
    from trading.compute.risk import check_order
    d = check_order(_order("buy"), **_ok())
    assert not d.blocked


# ============================================================================
# pre_open 消费点（block 全拦 / 总仓位 fail-closed / 总仓位逐单扣减）
# ============================================================================
def _green_signals():
    return [{
        "symbol": "300214.SZ",
        "order": {"symbol": "300214.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "formed_at": None, "stop_price": 9.0, "take_profit": 11.0, "max_wait": 5,
    }]


@pytest.fixture
def pre_open_env(risk_db, monkeypatch):
    """pre_open 集成隔离（复用 risk_db 的 DB 隔离）：plan dir + engine 实例。

    risk_control 表即 risk_db 建的隔离表——case 里 write_risk_control 直接写、
    pre_open 内 resolve_risk_control 读同一 _DEFAULT_DB。
    """
    monkeypatch.setenv("TRADE_PLAN_DIR", str(risk_db + ".plans"))
    from trading import position_book
    monkeypatch.setattr(position_book, "_DEFAULT_DB", risk_db)
    position_book.init_db()
    from trading.engine import TradingEngine
    return TradingEngine()


def _run_pre_open(eng, signals, gw=None):
    """统一 mock 链：gate 绿 + 撤单/超期/白名单 mock + signals 注入。

    _state_store 整体 mock（沿袭 test_pre_open_l1_halt 范式），但
    resolve_risk_control 显式透传真模块（risk_control 走隔离真 DB）。
    返回 (result, submit_mock)。
    """
    from trading import engine
    import trading.phases.pre_open as _pre_open_mod

    eng._pre_open_gate = AsyncMock(return_value=(True, ""))
    with patch("trading.trading_plan") as tp, \
         patch("trading.phases.pre_open._cancel_all_open_orders",
               new=AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
         patch("trading.phases.pre_open._scan_expired_positions", return_value=[]), \
         patch("trading.phases.pre_open._submit") as submit_mock, \
         patch("trading.phases.pre_open.get_gateway", return_value=gw):
        tp.load_plan.return_value = {"confirmed": True, "orders": []}
        submit_mock.return_value = {"state": "SUBMITTED", "order_id": "1"}
        with patch("trading.phases.pre_open._state_store") as ss:
            ss.resolve_risk_control = state_store.resolve_risk_control  # 透传真身
            ss.get_open_buy_amount = state_store.get_open_buy_amount    # 同上（额度基数走隔离真 DB）
            ss.list_signals_with_meta_by_plan_date.return_value = signals
            ss.build_trade_id.side_effect = lambda aid, sym, d: f"{aid}_{sym}_{d}"
            ss.get_account.return_value = {"account_id": "ACC"}
            ss.get_latest_action.return_value = "CONFIRMED"
            ss.has_order.return_value = False
            ss.insert_order.return_value = True
            result = asyncio.run(engine.pre_open("2026-07-31", ports=eng._ports))
    return result, submit_mock


def test_pre_open_block_skips_all_orders(pre_open_env):
    """block=ON → 挂单循环整体跳过（skip payload），_submit 绝不触达；存量管理已执行。"""
    state_store.write_risk_control(block_new_orders=True)
    result, submit_mock = _run_pre_open(pre_open_env, _green_signals())
    assert result["submitted"] == 0
    assert "人工风控开关" in result["skipped"]
    submit_mock.assert_not_called()


def test_pre_open_position_cap_failclosed_without_asset(pre_open_env):
    """max_pos<1.0 + gw=None（权益查不到）→ fail-closed 全跳过（pos_check_failed）。"""
    state_store.write_risk_control(max_total_position=0.8)
    result, submit_mock = _run_pre_open(pre_open_env, _green_signals(), gw=None)
    assert result["submitted"] == 0 and result.get("pos_check_failed") is True
    submit_mock.assert_not_called()


def test_pre_open_position_cap_skips_over_quota(pre_open_env):
    """额度不足单跳过（pos_capped 计数），额度内单照挂。"""
    state_store.write_risk_control(max_total_position=0.5)
    # quota = 0.5×100000 − 49900 = 100 → 单金额 1000 > 100 → 跳过
    gw = MagicMock()
    gw.query_asset = AsyncMock(return_value={
        "total_asset": 100000.0, "cash": 50000.0, "market_value": 49900.0})
    result, submit_mock = _run_pre_open(pre_open_env, _green_signals(), gw=gw)
    assert result["submitted"] == 0 and result.get("pos_capped") == 1
    submit_mock.assert_not_called()

    # 额度充足（quota = 0.9×100000−49900 = 40100 ≥ 1000）→ 照挂
    state_store.write_risk_control(max_total_position=0.9)
    result2, submit2 = _run_pre_open(pre_open_env, _green_signals(), gw=gw)
    assert result2["submitted"] == 1 and result2.get("pos_capped") == 0
    submit2.assert_called_once()


def test_pre_open_position_cap_counts_open_buy_orders(pre_open_env):
    """review I-2：额度基数计入未终态买入委托——已有挂单占额时新单被跳过。

    0.9×100000−49900=40100 名义额度，但 order 表有 39500 未终态买单占额
    → 剩余 600 < 本单 1000 → 跳过（原实现只扣市值会放行 = 名义敞口击穿上限）。
    """
    import sqlite3
    from trading.account import resolve_account_id
    # 隔离库插 account + 未终态 buy 委托（直接 SQL，绕过 FSM 最小构造）。
    # account_id 用 _resolve_account_id 真身口径（pre_open 额度查询走它，不对齐则查不到）
    _aid = resolve_account_id()
    db = state_store._DEFAULT_DB
    with sqlite3.connect(db) as con:
        con.execute("INSERT OR IGNORE INTO account(account_id, broker, created_at) "
                    "VALUES(?, 'qmt', '2026-08-17')", (_aid,))
        con.execute(
            'INSERT INTO "order"(order_id, trade_id, account_id, trade_date, symbol, '
            'side, purpose, qty, price, state) VALUES(?,?,?,?,?,?,?,?,?,?)',
            ("o1", "t1", _aid, "2026-07-31", "600000.SH", "buy", "OPEN",
             4000.0, 10.0, "SUBMITTED"))  # 4000×10 = 40000 占额
    state_store.write_risk_control(max_total_position=0.9)
    gw = MagicMock()
    gw.query_asset = AsyncMock(return_value={
        "total_asset": 100000.0, "cash": 50000.0, "market_value": 49900.0})
    result, submit_mock = _run_pre_open(pre_open_env, _green_signals(), gw=gw)
    # 40100 − open_buy(≈40000) ≈ 100 < 1000 → 跳过
    assert result["submitted"] == 0 and result.get("pos_capped") == 1
    submit_mock.assert_not_called()
