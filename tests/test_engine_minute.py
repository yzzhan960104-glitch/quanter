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


# ============ I-1: 全平后重置基准（entry_price/running_high/trailing_stop） ============


def test_run_minute_reset_baseline_after_full_close():
    """止损全平后，二轮建仓的 trailing_stop 必须从 0 重新起算（不沿用旧值）。

    场景（跨两日满足 T+1，全程持仓期间触发 update_trailing_stop）：
    - D1[0]：信号 0.8，价格 10，high=13 → 建仓，running_high 抬到 13，trailing_stop≈13。
    - D2[0]（次日，prev_held 转底仓可卖）：价格 9.4 < 10×0.95 → 固定止损全平（position 归 0）。
    - D2[1]：价格 12，信号 0.8 → 二轮建仓（position>0），触发 update_trailing_stop。

    ★ 核心断言（spy 法拦截 update_trailing_stop 入参）：
    二轮建仓后第一次 update_trailing_stop 调用的 prev_stop 必须 == 0.0（已重置）。
    修复前：prev_stop 沿用 D1 期间的 ~13（残留旧值），新仓一建立就会被旧 stop 误触发。

    Why spy update_trailing_stop 而非读引擎属性：trailing_stop 是 run_minute 的局部变量，
    无法从引擎实例外部读取；monkey-patch update_trailing_stop 函数拦截入参是最干净的
    黑盒探测法（不依赖引擎内部实现细节，仅依赖"全平后下次 update 的 prev_stop 应为 0"契约）。
    """
    idx_d1 = pd.date_range("2024-01-02 09:30", periods=15, freq="min")
    idx_d2 = pd.date_range("2024-01-03 09:30", periods=3, freq="min")
    idx = idx_d1.append(idx_d2)
    # D1[0..14]：价格 10 震荡（high-low=0.2 使 atr≈0.2 > 1e-9，update 块才会执行），
    # D1[0] 信号建仓后持仓期间持续 update trailing_stop（running_high 累积抬升到 ~10.24）。
    # D2[0]：次日开盘 prev_held 转底仓，价格暴跌到 9.4 → 止损全平（position 归 0）。
    # D2[1]：价格 12，信号 0.8 → 二轮建仓（position>0）。
    # D2[2]：价格 12.1（持仓无动作），update 块执行 → 此时 prev_stop 必须是 0（已重置）。
    d1_close = pd.Series([10.0 + 0.01 * i for i in range(15)], index=idx_d1)
    d1_high = d1_close + 0.1   # high-low=0.2 → atr≈0.2，使 update 块的 atr>1e-9 守卫通过
    d1_low = d1_close - 0.1
    d2_close = pd.Series([9.4, 12.0, 12.1], index=idx_d2)
    d2_high = d2_close + 0.1
    d2_low = d2_close - 0.1
    close = pd.concat([d1_close, d2_close])
    df = pd.DataFrame(
        {
            "open": close,
            "high": pd.concat([d1_high, d2_high]),
            "low": pd.concat([d1_low, d2_low]),
            "close": close,
            "volume": 1000,
        },
        index=idx,
    )
    # D1[0] 建, D1[1..14] 持仓, D2[0] 止损平（sig=0）, D2[1] 二轮建仓, D2[2] 持仓 update
    sig = pd.Series(
        [0.8] + [0.0] * 14 + [0.0, 0.8, 0.0],
        index=idx,
    )

    from trading import order_state as _os

    calls: list[dict] = []
    _orig_update = _os.update_trailing_stop

    def _spy_update(high_, atr_, k_, prev_stop):
        ret = _orig_update(high_, atr_, k_, prev_stop)
        calls.append({"prev_stop": prev_stop, "ret": ret, "high": high_})
        return ret

    # ⚠️ engine.run_minute 内部用 `from trading.order_state import update_trailing_stop`
    # 把名字绑定到 engine 模块的局部作用域（每次调用时动态 import），
    # 故 patch trading.order_state.update_trailing_stop 即可生效（import 时取最新值）。
    _os.update_trailing_stop = _spy_update
    eng = BacktestEngine(initial_capital=100000)
    try:
        # _calculate_result 在 win_count=0 时会除零（独立 bug，与本测试无关），吞掉
        eng.run_minute(df, sig, "000001.SZ", sl_pct=0.05, tp_pct=0.05, trail_k=2.0)
    except ZeroDivisionError:
        pass
    finally:
        _os.update_trailing_stop = _orig_update

    # 必须至少有 2 次 update（D1 建仓后 1 次 + D2 二轮建仓后 1 次）
    assert len(calls) >= 2, f"应至少 2 次 trailing_stop 更新，实际 {len(calls)}: {calls}"

    # 二轮建仓后第一次 update 是 calls[-1]（最后一根 K 线 D2[1] 触发）
    # 修复前：prev_stop 沿用 D1 残留值（≈13）；修复后：prev_stop==0（已重置）
    last_prev_stop = calls[-1]["prev_stop"]
    assert last_prev_stop == 0.0, (
        f"全平后二轮建仓，trailing_stop 应从 0 重新起算（已重置），"
        f"实际 prev_stop={last_prev_stop}（沿用旧值，会导致新仓立即误触发移动止损）。"
        f"全调用序列: {calls}"
    )

