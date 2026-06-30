"""回测引擎 event_emitter 注入：成交/进度/风控事件，默认 None 不破坏现有行为。

设计意图：
- 为 Epic 4（SSE 实时回测流）打通引擎层事件出口。
- progress 事件用于前端进度条/实时净值推送。
- trade 事件用于实时成交回报展示。
- risk 事件（失败成交：涨跌停/资金不足）用于风控告警。
- 默认 event_emitter=None → 零开销、零行为变化，保证所有既有调用方无须改动。
"""
import numpy as np
import pandas as pd
from backtest.engine import BacktestEngine


def _make_data():
    """构造 30 日单调上升行情 + 上升信号，确保至少触发一次买入成交。"""
    idx = pd.date_range("2024-01-01", periods=30)
    df = pd.DataFrame({
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": np.linspace(10.0, 11.0, 30),
        "volume": 10000,
    }, index=idx)
    signal = pd.Series(np.linspace(0.2, 0.8, 30), index=idx)
    return df, signal


def test_default_none_unchanged():
    """不传 emitter 时，run 行为与契约完全不变（返回结果含既定字段）。"""
    df, signal = _make_data()
    result = BacktestEngine().run(df, signal, "000001.SZ")  # 不传 emitter
    # 既有结果字典契约：至少含 metrics / nav / trades 之一
    assert "metrics" in result or "nav" in result or "trades" in result


def test_emitter_receives_progress():
    """emitter 必须收到 progress 事件（每日一发）。"""
    df, signal = _make_data()
    events = []
    BacktestEngine().run(
        df, signal, "000001.SZ",
        event_emitter=lambda ev: events.append(ev),
    )
    types = {e["type"] for e in events}
    assert "progress" in types  # 进度事件必发
    # 30 个交易日应至少发 30 个 progress
    progress_events = [e for e in events if e["type"] == "progress"]
    assert len(progress_events) == 30
    # 字段完备性
    p0 = progress_events[0]
    assert {"type", "date", "i", "n", "nav"} <= set(p0.keys())
    assert p0["n"] == 30
    assert p0["i"] == 0


def test_emitter_receives_trade_event():
    """上升信号段应至少触发一次成交，emitter 必须收到 trade 事件。"""
    df, signal = _make_data()
    trade_events = []
    BacktestEngine().run(
        df, signal, "000001.SZ",
        event_emitter=lambda ev: trade_events.append(ev) if ev["type"] == "trade" else None,
    )
    # 信号上升段应至少触发一次买入成交
    assert len(trade_events) >= 1
    # 字段完备性
    t0 = trade_events[0]
    assert {"type", "date", "direction", "shares", "price", "symbol"} <= set(t0.keys())
    assert t0["symbol"] == "000001.SZ"
    assert t0["direction"] == "buy"
    assert t0["shares"] > 0
    assert t0["price"] > 0
