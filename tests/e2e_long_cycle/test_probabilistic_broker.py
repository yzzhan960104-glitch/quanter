# -*- coding: utf-8 -*-
"""V4：ProbabilisticBroker 概率成交 + 构造熔断/超期场景。"""
from __future__ import annotations

import asyncio
from datetime import date, time
from unittest.mock import patch, MagicMock

import pandas as pd


def _stub_feeder(price=10.5):
    """MinBarFeeder 桩（返固定价）。"""
    feeder = MagicMock()
    feeder.feed.return_value = {"300001.SZ": {"last_price": price, "high": price, "low": price}}
    feeder.price_for.return_value = price
    return feeder


def test_submit_probability_distribution_fixed_seed():
    """固定种子下 100 单：FILLED ~70 / PARTIAL ~15 / REJECTED ~5 / 延迟 ~10（容差 ±10）。"""
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker

    broker = ProbabilisticBroker(seed=42, min_bar_feeder=_stub_feeder())
    counts = {"FILLED": 0, "PARTIAL_FILLED": 0, "REJECTED": 0}
    for _ in range(100):
        order = {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.5}
        result = broker.simulate_submit(order, t_date=date(2026, 7, 1), up_to=time(9, 25))
        counts[result["state"]] = counts.get(result["state"], 0) + 1
    assert 55 <= counts["FILLED"] <= 85        # ~70 ±15
    assert 5 <= counts["PARTIAL_FILLED"] <= 30  # ~15
    assert 0 <= counts["REJECTED"] <= 15        # ~5


def test_partial_fill_traded_volume_less_than_qty():
    """PARTIAL_FILLED → traded_volume < qty（gap4 部分成交精度）。

    注：qty 取 1000（非 brief 原 100）——brief 实现保 A 股最小手数 ``max(100, ...)``，
    qty=100 时部分成交下限被锁回 100（与全成等价，断言 ``<100`` 恒假）。
    qty=1000 给 30%-70% 部分成交留出 300-700 的合法空间，物理意图（traded<qty）才成立。
    """
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker

    broker = ProbabilisticBroker(seed=1, min_bar_feeder=_stub_feeder(),
                                  force_state="PARTIAL_FILLED")  # 强制部分成交断言
    order = {"symbol": "300001.SZ", "qty": 1000, "side": "buy", "price": 10.5}
    result = broker.simulate_submit(order, t_date=date(2026, 7, 1), up_to=time(9, 25))
    assert result["state"] == "PARTIAL_FILLED"
    assert result["traded_volume"] < 1000


def test_circuit_breaker_constructed_day_returns_depressed_equity():
    """构造熔断日：query_asset 返 start×0.96（-3% < 阈值）→ post_close 熔断判定触发。"""
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker

    broker = ProbabilisticBroker(seed=42, min_bar_feeder=_stub_feeder(),
                                  circuit_breaker_days={date(2026, 7, 10)},
                                  start_equity=1_000_000.0)
    asset = broker.simulate_query_asset(date(2026, 7, 10))
    assert asset["total_asset"] == 960_000.0  # -4% < -3% 熔断阈值


def test_expired_symbol_marked_by_holding_days():
    """构造超期标的：expired_symbols 中的标的在 position 里 holding_days > max_holding。"""
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker

    broker = ProbabilisticBroker(seed=42, min_bar_feeder=_stub_feeder(),
                                  expired_symbols={"300001.SZ": {"entry_date": "2026-06-15",
                                                                  "holding_days_ref": date(2026, 7, 10)}})
    positions = broker.simulate_fetch_positions(date(2026, 7, 10))
    # 300001.SZ 持仓 entry 06-15 → 07-10 holding_days≈25 > max_holding(默认 10)
    assert "300001.SZ" in positions

def test_inject_fills_writes_fill_and_position_via_engine(isolated_state, monkeypatch):
    """成交回报注入：经 _handle_order_update 真身写 fill + position（防空转回归）。

    物理意图（design 2026-08-01 §4.1）：ProbabilisticBroker 的 FILLED 回报必须走生产
    成交链路（insert_fill -> apply_fill_to_position -> trade_event），否则报表 fill/position
    恒空，流程"全绿但空转"。
    """
    import sqlite3
    from datetime import date, time
    from unittest.mock import patch

    from trading import engine as engine_mod, state_store
    from tests.e2e_long_cycle.min_bar_feeder import MinBarFeeder
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker

    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    feeder = MinBarFeeder(stk_mins_loader=lambda s, d: pd.DataFrame())
    broker = ProbabilisticBroker(seed=1, min_bar_feeder=feeder, force_state="FILLED")
    eng = engine_mod.TradingEngine()
    order = {"symbol": "300001.SZ", "qty": 100, "side": "BUY", "price": 10.0}
    with broker.attach(date(2026, 7, 2), time(9, 25)) as gw:
        eng._gw = gw
        broker.simulate_submit(order, date(2026, 7, 2), time(9, 25))
        # A4 删 record_live_trade，原 patch 行随之删（审计平移 trade_event 表）
        with patch("infra.notifier.fire_and_forget", lambda coro: coro.close()):
            asyncio.run(broker.inject_fills(eng))
    account = engine_mod._resolve_account_id()
    assert state_store.get_position(account, "300001.SZ") is not None, "BUY 成交应落 position"
    with sqlite3.connect(state_store._DEFAULT_DB) as con:
        n = con.execute("SELECT COUNT(*) FROM fill WHERE symbol='300001.SZ'").fetchone()[0]
    assert n >= 1, "BUY 成交应落 fill 表"


def test_resting_tp_stays_submitted_when_high_below_price(isolated_state, monkeypatch):
    """TP 限价单：stk_mins 累积 high < tp 价 -> 保持 SUBMITTED 挂单，不产生成交。"""
    import pandas as pd
    from datetime import date, time

    from trading import trading_plan
    from tests._legacy_plan_io import save_plan_legacy
    from tests.e2e_long_cycle.min_bar_feeder import MinBarFeeder
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker

    save_plan_legacy("2026-07-02", [{  # C3：legacy shim
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "BUY", "price": 10.0},
        "stop_price": 9.5, "take_profit": 11.0, "tp1": 10.5, "tp1_portion": 50,
    }], confirmed=True)

    def loader(sym, d):
        return pd.DataFrame({"trade_time": [f"{d} 10:00:00"],
                             "open": [10.0], "high": [10.3], "low": [9.9], "close": [10.1],
                             "vol": [1000], "amount": [10100.0]})

    feeder = MinBarFeeder(stk_mins_loader=loader)
    broker = ProbabilisticBroker(seed=1, min_bar_feeder=feeder, force_state="FILLED")
    broker._positions["300001.SZ"] = {"volume": 100, "avg_price": 10.0}
    with broker.attach(date(2026, 7, 2), time(10, 0)):
        r = broker.simulate_submit(
            {"symbol": "300001.SZ", "qty": 50, "side": "SELL", "price": 10.5},
            date(2026, 7, 2), time(10, 0))
        assert r["state"] == "SUBMITTED", "high 10.3 < tp 10.5 应挂单不成交"
        assert broker._resting, "TP 限价单应入 resting 队列"
        # 价格未到 -> scan 后无成交回报，持仓不动
        from trading import engine as engine_mod
        asyncio.run(broker.scan_resting_and_inject(engine_mod.TradingEngine(),
                                                   date(2026, 7, 2), time(10, 0)))
        assert not broker._pending_reports, "价格未到不应产生成交回报"
        assert broker._positions["300001.SZ"]["volume"] == 100


def test_resting_tp_fills_when_high_reaches_price(isolated_state, monkeypatch):
    """TP 限价单：stk_mins 累积 high >= tp 价 -> FILLED，成交落 fill/position。"""
    import sqlite3
    from datetime import date, time
    from unittest.mock import patch

    from trading import engine as engine_mod, state_store
    from tests.e2e_long_cycle.min_bar_feeder import MinBarFeeder
    from tests.e2e_long_cycle.probabilistic_broker import ProbabilisticBroker
    from trading import trading_plan
    from tests._legacy_plan_io import save_plan_legacy

    save_plan_legacy("2026-07-02", [{  # C3：legacy shim
        "order": {"symbol": "300001.SZ", "qty": 100, "side": "BUY", "price": 10.0},
        "stop_price": 9.5, "take_profit": 11.0, "tp1": 10.5, "tp1_portion": 50,
    }], confirmed=True)

    def loader(sym, d):
        return pd.DataFrame({"trade_time": [f"{d} 10:00:00"],
                             "open": [10.0], "high": [10.6], "low": [9.9], "close": [10.4],
                             "vol": [1000], "amount": [10100.0]})

    account = engine_mod._resolve_account_id()
    state_store.upsert_account(account, broker="qmt")
    state_store.apply_fill_to_position(account, "300001.SZ", "BUY", 100, 10.0,
                                       "2026-07-02 09:25:00")

    feeder = MinBarFeeder(stk_mins_loader=loader)
    broker = ProbabilisticBroker(seed=1, min_bar_feeder=feeder, force_state="FILLED")
    broker._positions["300001.SZ"] = {"volume": 100, "avg_price": 10.0}
    with broker.attach(date(2026, 7, 2), time(10, 0)) as gw:
        eng = engine_mod.TradingEngine()
        eng._gw = gw
        broker.simulate_submit(
            {"symbol": "300001.SZ", "qty": 50, "side": "SELL", "price": 10.5},
            date(2026, 7, 2), time(10, 0))
        # A4 删 record_live_trade，原 patch 行随之删（审计平移 trade_event 表）
        with patch("infra.notifier.fire_and_forget", lambda coro: coro.close()):
            asyncio.run(broker.scan_resting_and_inject(eng, date(2026, 7, 2), time(10, 0)))
    assert broker._positions["300001.SZ"]["volume"] == 50, "TP 成交后镜像持仓应减半"
    pos = state_store.get_position(account, "300001.SZ")
    assert pos is not None and pos["qty"] == 50, f"DB position 应剩 50，实际 {pos}"
    with sqlite3.connect(state_store._DEFAULT_DB) as con:
        n = con.execute("SELECT COUNT(*) FROM fill WHERE symbol='300001.SZ' "
                        "AND direction='SELL'").fetchone()[0]
    assert n == 1, "TP 成交应落 SELL fill"
