"""Task 15: 引擎分钟级回测 + T+1 底仓冻结 + 止损止盈移动止损 + event_emitter。

设计意图（Why）：
- 分钟级回测用于宏观 CTA 策略的精细化回放——日级回测无法刻画盘中止损止盈的"穿越即触发"
  物理时序，必须下放到分钟（或更细）粒度。
- A 股 T+1 制度：当日新仓不可卖，必须冻结至次日。引擎显式区分"底仓（昨日及更早）"与
  "冻结仓（今日新买）"，确保卖出分支只动底仓，杜绝回测里"当日买当日卖"的违规撮合。
- event_emitter 默认 None → 零行为变化（与 run/run_portfolio 完全对称）。
"""
import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine, _split_t1


def _up_data():
    """构造 120 分钟单调上升行情 + 第 20 根后给出 0.8 信号，确保至少触发一次买入成交。

    价格 10 → 11.5 单调上行：买入后不会触发止损/止盈，纯粹验证建仓 + 进度 + 成交事件。
    """
    idx = pd.date_range("2024-01-02 09:30", periods=120, freq="min")
    close = pd.Series(np.linspace(10, 11.5, 120), index=idx)
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": 1000,
        },
        index=idx,
    )
    # 第 20 根开始信号转 0.8（多头），此前为 0（空仓）
    sig = pd.Series(np.where(np.arange(120) >= 20, 0.8, 0.0), index=idx)
    return df, sig


# ============ T+1 底仓冻结纯函数 ============


def test_split_t1_sellable_is_prev_held_frozen_is_today_bought():
    """_split_t1 语义辨析：底仓可卖=prev_held，今日新仓冻结=today_bought。

    brief 测试断言 _split_t1(current_held=0, today_bought=100, prev_held=200) == (200, 100)：
    - 含义：昨日持仓 200 是底仓，可日内卖出；今日新买的 100 冻结至次日。
    - current_held 参数在此语义下是冗余的（保留签名仅为向后兼容 / 调用方便），
      真实物理判定只依赖 prev_held（底仓）与 today_bought（新仓）。
    """
    sellable, frozen = _split_t1(current_held=0, today_bought=100, prev_held=200)
    assert sellable == 200  # 昨日 200 为底仓可卖
    assert frozen == 100    # 今日新买的 100 冻结


def test_split_t1_zero_prev_zero_today():
    """无底仓、无新仓：可卖与冻结均为 0。"""
    sellable, frozen = _split_t1(current_held=0, today_bought=0, prev_held=0)
    assert sellable == 0
    assert frozen == 0


def test_split_t1_only_prev_no_new_buy():
    """只有底仓、今日无新买：全部为底仓可卖，冻结为 0。"""
    sellable, frozen = _split_t1(current_held=500, today_bought=0, prev_held=500)
    assert sellable == 500
    assert frozen == 0


def test_split_t1_today_new_buy_only_no_prev():
    """无底仓、今日全为新买：底仓可卖 0，全部冻结。"""
    sellable, frozen = _split_t1(current_held=300, today_bought=300, prev_held=0)
    assert sellable == 0
    assert frozen == 300


# ============ run_minute 默认 None 不破坏契约 ============


def test_run_minute_default_none_unaffected():
    """不传 emitter：run_minute 返回结果字典，至少含 trades / daily_records。"""
    df, sig = _up_data()
    r = BacktestEngine(initial_capital=100000).run_minute(df, sig, "000001.SZ")
    assert isinstance(r, dict)
    assert "trades" in r
    assert "daily_records" in r


def test_run_minute_returns_complete_contract():
    """run_minute 结果字典必须复用 _calculate_result 的完整契约字段。"""
    df, sig = _up_data()
    r = BacktestEngine().run_minute(df, sig, "000001.SZ")
    for field in (
        "initial_capital",
        "final_nav",
        "total_return",
        "max_drawdown",
        "sharpe_ratio",
        "trades",
        "daily_records",
    ):
        assert field in r, f"缺失字段: {field}"


# ============ run_minute event_emitter 注入 ============


def test_run_minute_emitter_receives_progress_and_trade():
    """emitter 必须收到 progress（每分钟一发）+ 至少一帧 trade（建仓成交）。"""
    df, sig = _up_data()
    events = []
    BacktestEngine().run_minute(
        df, sig, "000001.SZ",
        event_emitter=lambda e: events.append(e),
    )
    types = {e["type"] for e in events}
    assert "progress" in types
    assert "trade" in types
    # 120 根分钟 K 线 → 至少 120 个 progress
    progress_events = [e for e in events if e["type"] == "progress"]
    assert len(progress_events) == 120
    # 字段完备性
    p0 = progress_events[0]
    assert {"type", "date", "i", "n", "nav"} <= set(p0.keys())
    assert p0["n"] == 120
    # 至少一帧 trade
    trade_events = [e for e in events if e["type"] == "trade"]
    assert len(trade_events) >= 1
    t0 = trade_events[0]
    assert {"type", "date", "direction", "shares", "price", "symbol"} <= set(t0.keys())
    assert t0["symbol"] == "000001.SZ"
    assert t0["direction"] == "buy"
    assert t0["shares"] > 0


# ============ run_minute T+1 冻结 + 止损止盈路径 ============


def test_run_minute_take_profit_emits_risk_event():
    """止盈触发时必须发 risk 帧（level=WARN, reason 含"触及止损/止盈"）。

    构造手法：
    - 第 0 根买入（信号 0.8），随后价格快速上涨 6% 触发 5% 止盈。
    - 但 T+1：买入当日不能卖，必须跨日（次日）才能触发底仓卖出。
    - 用两日分钟序列：D1 建仓，D2 价格已达止盈线 → 卖出 + risk 帧。
    """
    idx_d1 = pd.date_range("2024-01-02 09:30", periods=10, freq="min")
    idx_d2 = pd.date_range("2024-01-03 09:30", periods=10, freq="min")
    idx = idx_d1.append(idx_d2)
    # D1 价格 10 → 10.1（建仓后小幅波动），D2 价格跳到 10.6（+6% > 5% 止盈）
    close = pd.Series(
        [10.0 + 0.01 * i for i in range(10)] + [10.6 + 0.001 * i for i in range(10)],
        index=idx,
    )
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.01,
            "low": close - 0.01,
            "close": close,
            "volume": 1000,
        },
        index=idx,
    )
    # D1 全程 0.8 信号建仓，D2 信号仍 0.8（持仓不卖），靠止盈被动卖出
    sig = pd.Series(0.8, index=idx)

    events = []
    r = BacktestEngine(initial_capital=100000).run_minute(
        df, sig, "000001.SZ",
        sl_pct=0.05, tp_pct=0.05,
        event_emitter=lambda e: events.append(e),
    )
    risk_events = [e for e in events if e["type"] == "risk"]
    # D2 价格涨破止盈线 → 必须有 risk 帧（止盈离场）
    assert len(risk_events) >= 1, "止盈触发应至少发一帧 risk 事件"
    r0 = risk_events[0]
    assert r0["level"] == "WARN"
    assert "止损" in r0["reason"] or "止盈" in r0["reason"]
    # 至少有一笔卖出成交
    trade_events = [e for e in events if e["type"] == "trade"]
    sell_events = [e for e in trade_events if e["direction"] == "sell"]
    assert len(sell_events) >= 1, "止盈触发应至少有一笔卖出"


def test_run_minute_progress_nav_finite():
    """progress 帧的 nav 必须永远是有限数（防 NaN/Inf 经 SSE 透传成非法 JSON）。"""
    df, sig = _up_data()
    events = []
    BacktestEngine().run_minute(
        df, sig, "000001.SZ",
        event_emitter=lambda e: events.append(e),
    )
    progress_events = [e for e in events if e["type"] == "progress"]
    assert len(progress_events) >= 1
    for p in progress_events:
        assert isinstance(p["nav"], (int, float))
        assert np.isfinite(p["nav"]), f"progress nav 出现 NaN/Inf: {p['nav']}"
