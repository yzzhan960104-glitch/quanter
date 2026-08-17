# -*- coding: utf-8 -*-
"""⑦ 存量持仓止损裸奔修复测试（2026-08-17）。

背景（双轴评审发现的结构缺陷）：
    _stoploss 原只把「今日 plan_date 的 SIGNAL」装进 monitor_ctx/stop_prices；
    持仓成交后 cooldown=8 挡住 eod 重产同标的信号 → 次日起不在今日 SIGNAL →
    monitor「无止损价」continue → 盘中个股止损对存量持仓完全不生效。
    decide_exit 主路径实际只覆盖成交当日。

修复契约：
    1. 按当前真实持仓（gw.query_positions volume>0）反查最新 SIGNAL meta 注入三 map
       （今日 SIGNAL 已覆盖的 sym 不重查不覆盖——新计划优先）；
    2. 持仓反查不塞 pending_ctx（cancel_on 是入场时挂单阈值，今日无该 pending 单）；
    3. decide_cfg per-signal：SIGNAL.meta.exec_params（实验口径定终身）> env 基线；
    4. list_active_holding_signals：每 symbol 取 event_id 最大一条（最新计划优先）。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading import state_store
from trading.engine import TradingEngine
from trading.stop_loss_context import StopLossContext

HOLDING_SYM = "600519.SH"

# 入场时 SIGNAL meta（带 exec_params 快照——单源收敛后的新数据形态）
HOLDING_META = {
    "symbol": HOLDING_SYM,
    "trade_id": f"ACC1_{HOLDING_SYM}_2026-08-05",
    "order": {"symbol": HOLDING_SYM, "qty": 100, "side": "buy", "price": 1500.0},
    "stop_price": 1450.0, "take_profit": 1600.0, "neckline": 1490.0, "atr": 40.0,
    "tp1": 1540.0, "tp1_portion": 0.3, "cancel_on": 1580.0,
    "formed_at": "2026-08-04", "max_wait": 8,
    "exec_params": {
        "stop_atr_mult": 1.0, "tp_h_mult": 2.5, "tp1_h_mult": 1.0, "tp1_portion": 0.3,
        "max_wait": 8, "cancel_thresh_mult": 2.0, "max_holding": 20,
        "trailing_grace": 0, "trailing_step": 0.0, "trailing_floor": 0.0,
    },
}


def _run_stoploss(monkeypatch, tmp_path, *, signals, positions, holding_meta):
    """隔离 DB + mock 链跑 eng._stoploss()，返回 monitor 收到的 StopLossContext。"""
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    state_store.init_store()
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")

    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    gw.query_positions = AsyncMock(return_value=positions)

    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine.calendar") as cal, \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon, \
         patch("trading.engine._position_book") as pb:
        cal.is_trading_day.return_value = True
        cal.today.return_value = "2026-08-17"
        pb.get_entry_dates.return_value = {HOLDING_SYM: "2026-08-05"}
        with patch("trading.engine._state_store") as ss:
            # 真身模块上挂 mock（build_trade_id/is_trade_confirmed 走真 DB 语义）
            ss.list_signals_with_meta_by_plan_date.return_value = signals
            ss.build_trade_id.side_effect = lambda aid, sym, d: f"{aid}_{sym}_{d}"
            ss.is_trade_confirmed.return_value = True
            ss.list_active_holding_signals.return_value = holding_meta
            asyncio.run(eng._stoploss())
    ctx = mon.call_args.args[0]
    assert isinstance(ctx, StopLossContext)
    return ctx


def test_stoploss_backfills_holding_not_in_today_signals(monkeypatch, tmp_path):
    """持仓不在今日 SIGNAL → 反查注入 monitor_ctx（state.stop=入场快照）。"""
    ctx = _run_stoploss(
        monkeypatch, tmp_path,
        signals=[],                                   # 今日无计划
        positions={HOLDING_SYM: {"volume": 100, "avg_price": 1500.0}},
        holding_meta=[HOLDING_META])
    assert ctx.stop_prices == {HOLDING_SYM: 1450.0}
    mc = ctx.monitor_ctx[HOLDING_SYM]
    assert mc["state"]["stop"] == 1450.0
    assert mc["state"]["neckline"] == 1490.0 and mc["state"]["atr"] == 40.0
    # 持仓反查不塞 pending_ctx（今日无该 pending 买单）
    assert (ctx.pending_ctx or {}) == {}


def test_stoploss_holding_uses_exec_params_not_env(monkeypatch, tmp_path):
    """反查 SIGNAL 带 exec_params → decide_cfg 用实验口径（max_holding=20、trailing 全 0）。"""
    ctx = _run_stoploss(
        monkeypatch, tmp_path,
        signals=[],
        positions={HOLDING_SYM: {"volume": 100}},
        holding_meta=[HOLDING_META])
    cfg = ctx.monitor_ctx[HOLDING_SYM]["cfg"]
    assert cfg["max_holding"] == 20          # env 缺省 15
    assert cfg["tp1_portion"] == pytest.approx(0.3)   # env 缺省 0.5
    assert cfg["trailing_grace"] == 0 and cfg["trailing_step"] == 0.0  # env 5/0.1


def test_stoploss_holding_legacy_meta_falls_back_to_env(monkeypatch, tmp_path):
    """老 SIGNAL 无 exec_params → cfg 走 env 基线（grace=5/max_holding=15，现行为兼容）。"""
    legacy = {k: v for k, v in HOLDING_META.items() if k != "exec_params"}
    ctx = _run_stoploss(
        monkeypatch, tmp_path,
        signals=[], positions={HOLDING_SYM: {"volume": 100}},
        holding_meta=[legacy])
    cfg = ctx.monitor_ctx[HOLDING_SYM]["cfg"]
    assert cfg["max_holding"] == 15 and cfg["trailing_grace"] == 5


def test_stoploss_today_signal_takes_priority_over_backfill(monkeypatch, tmp_path):
    """持仓同时有今日 SIGNAL → 用今日（不触发反查）。"""
    today_meta = {
        "symbol": HOLDING_SYM,
        "order": {"symbol": HOLDING_SYM, "qty": 100, "side": "buy", "price": 10.0},
        "stop_price": 9.5, "take_profit": 12.0, "neckline": 10.0, "atr": 0.5,
        "tp1": 11.0,
        "exec_params": {"max_holding": 20},
    }
    eng_calls = {}
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    state_store.init_store()
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    gw.query_positions = AsyncMock(return_value={HOLDING_SYM: {"volume": 100}})
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.engine.calendar") as cal, \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon, \
         patch("trading.engine._position_book") as pb:
        cal.is_trading_day.return_value = True
        cal.today.return_value = "2026-08-17"
        pb.get_entry_dates.return_value = {}
        with patch("trading.engine._state_store") as ss:
            ss.list_signals_with_meta_by_plan_date.return_value = [today_meta]
            ss.build_trade_id.side_effect = lambda aid, sym, d: f"{aid}_{sym}_{d}"
            ss.is_trade_confirmed.return_value = True
            ss.list_active_holding_signals.side_effect = \
                lambda *a, **kw: eng_calls.setdefault("called", True) and []
            asyncio.run(eng._stoploss())
    ctx = mon.call_args.args[0]
    assert ctx.monitor_ctx[HOLDING_SYM]["cfg"]["max_holding"] == 20
    assert "called" not in eng_calls   # 今日 SIGNAL 已覆盖 → 反查未触发


# ============================================================================
# ⑧ scan_expired_positions per-symbol max_holding（新 20 / 老 15 过渡）
# ============================================================================
def test_scan_expired_per_symbol_dict(monkeypatch):
    """dict 口径：新持仓（20）窗口内不标、老持仓（15）按 15 标、缺键跳过。"""
    from trading.phases import stop_loss as _sl
    monkeypatch.setattr(_sl._position_book, "get_entry_dates",
                        lambda: {"NEW.SH": "2099-01-01", "OLD.SH": "2099-01-01",
                                 "NOCFG.SH": "2099-01-01"})
    # holding_days 桩确定性返 16（test_engine.py 边界用例同款范式，避开交易日历漂移）
    monkeypatch.setattr(_sl, "_trading_days_between", lambda s, e: 16)
    expired = _sl.scan_expired_positions(
        "2099-01-21", {"NEW.SH": 20, "OLD.SH": 15})
    syms = [e["symbol"] for e in expired]
    assert syms == ["OLD.SH"]                       # 16>15 标，16<=20 窗口内
    assert expired[0]["max_holding"] == 15          # per-symbol 值透传（播报/日志用）
    # 缺键（NOCFG.SH）保守跳过——即使超任何口径也不标
    expired2 = _sl.scan_expired_positions("2099-01-21", {"NEW.SH": 20})
    assert expired2 == []
    # int 口径（历史行为）零变化
    expired3 = _sl.scan_expired_positions("2099-01-21", 15)
    assert set(e["symbol"] for e in expired3) == {"NEW.SH", "OLD.SH", "NOCFG.SH"}


# ============================================================================
# state_store.list_active_holding_signals 单测（真 DB）
# ============================================================================

def test_list_active_holding_signals_picks_latest(tmp_path, monkeypatch):
    """同 symbol 多条 SIGNAL → 取 event_id 最大（最新计划口径）。"""
    import sqlite3
    db = str(tmp_path / "s.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    state_store.init_store()
    old_meta = {"stop_price": 9.0, "neckline": 10.0, "atr": 0.5, "take_profit": 12.0, "tp1": 11.0}
    new_meta = {**old_meta, "stop_price": 9.5}
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO account(account_id, broker, created_at) "
                    "VALUES('ACC1', 'qmt', '2026-08-01')")
        con.execute("INSERT INTO trade_event(event_id, account_id, trade_id, symbol, "
                    "action, meta, timestamp) VALUES(1, 'ACC1', "
                    "'ACC1_600519.SH_2026-07-20', '600519.SH', 'SIGNAL', ?, '2026-07-20T18:00:00')",
                    (json.dumps(old_meta),))
        con.execute("INSERT INTO trade_event(event_id, account_id, trade_id, symbol, "
                    "action, meta, timestamp) VALUES(2, 'ACC1', "
                    "'ACC1_600519.SH_2026-08-05', '600519.SH', 'SIGNAL', ?, '2026-08-05T18:00:00')",
                    (json.dumps(new_meta),))
    out = state_store.list_active_holding_signals("ACC1", ["600519.SH"])
    assert len(out) == 1
    assert out[0]["stop_price"] == 9.5            # 最新一条
    assert out[0]["trade_id"].endswith("2026-08-05")
    # 无命中的 symbol / 空集合
    assert state_store.list_active_holding_signals("ACC1", ["000001.SZ"]) == []
    assert state_store.list_active_holding_signals("ACC1", []) == []
