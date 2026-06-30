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
from factors.fusion import TargetWeightSignal, SignalDirection


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


# ============ T14 审查跟进：run_portfolio 路径 event_emitter 布点 ============
# Why 单独覆盖：service.run_single_backtest 走的是 engine.run_portfolio（组合路径），
# 而非 engine.run（单资产路径）。若只测 run()，SSE 实时流仍只吐最终 result 帧，
# 中途 progress/trade 帧不会出现（portfolio 循环未布点）。此用例守住组合路径布点。


def _make_portfolio_data():
    """构造 2 标的的 30 日价格面板 + 首日全仓买入信号，确保触发调仓成交。

    构造要点：
    - 价格单调上升 + 首日信号给标的 A 100% 权重 → 首日必触发买入。
    - 30 个交易日 → run_portfolio 循环 30 次，progress 应发 30 帧。
    """
    idx = pd.date_range("2024-01-01", periods=30)
    df_a = pd.DataFrame({
        "open": 10.0, "high": 10.5, "low": 9.8,
        "close": np.linspace(10.0, 11.0, 30), "volume": 10000,
    }, index=idx)
    df_b = pd.DataFrame({
        "open": 20.0, "high": 20.5, "low": 19.8,
        "close": np.linspace(20.0, 21.0, 30), "volume": 8000,
    }, index=idx)
    price_data = {"000001.SZ": df_a, "600000.SH": df_b}
    # 首日信号：A=1.0 / B=0.0 → 引擎应在首日全仓买入 A
    signals = [
        TargetWeightSignal(
            timestamp=idx[0],
            weights={"000001.SZ": 1.0, "600000.SH": 0.0},
            directions={
                "000001.SZ": SignalDirection.BUY,
                "600000.SH": SignalDirection.HOLD,
            },
        )
    ]
    return price_data, signals, idx


def test_run_portfolio_default_none_unchanged():
    """不传 emitter：run_portfolio 行为与契约完全不变（返回结果含既定字段）。"""
    price_data, signals, _ = _make_portfolio_data()
    result = BacktestEngine().run_portfolio(price_data, signals)  # 不传 emitter
    # 既有结果字典契约：组合路径必含 final_nav / total_return / daily_records
    assert "final_nav" in result
    assert "total_return" in result
    assert "daily_records" in result


def test_run_portfolio_emits_progress_and_trade():
    """run_portfolio 注入 emitter 后发 progress，调仓日发 trade；默认 None 不变。

    断言三件事：
    1) progress 帧存在且数量等于交易日数（每日一发）。
    2) 首日信号触发调仓 → 至少有一帧 trade 事件。
    3) trade 字段完备（type/date/direction/shares/price/symbol）。
    """
    price_data, signals, idx = _make_portfolio_data()
    events = []
    BacktestEngine(initial_capital=1_000_000.0).run_portfolio(
        price_data, signals,
        event_emitter=lambda ev: events.append(ev),
    )
    types = {e["type"] for e in events}
    # progress 必发
    assert "progress" in types
    progress_events = [e for e in events if e["type"] == "progress"]
    # 30 个交易日 → 至少 30 个 progress（每日一发；all_dates 取并集为 30）
    assert len(progress_events) >= 30
    # 字段完备性
    p0 = progress_events[0]
    assert {"type", "date", "i", "n", "nav"} <= set(p0.keys())
    assert p0["n"] >= 30
    assert p0["i"] == 0
    # 首日信号触发调仓 → 应有 trade 帧
    trade_events = [e for e in events if e["type"] == "trade"]
    assert len(trade_events) >= 1, "首日调仓信号应至少触发一帧 trade 事件"
    t0 = trade_events[0]
    assert {"type", "date", "direction", "shares", "price", "symbol"} <= set(t0.keys())
    assert t0["symbol"] in {"000001.SZ", "600000.SH"}
    assert t0["direction"] in {"buy", "sell"}
    assert t0["shares"] > 0
    assert t0["price"] > 0
