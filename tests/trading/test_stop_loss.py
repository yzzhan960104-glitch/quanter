# -*- coding: utf-8 -*-
"""海龟 trailing 止损离散纯函数单测（Task 2）+ should_trigger_stop 拆解单测（阶段5）。"""
from trading.compute.stop import compute_stop_price  # Layer2 阶段6：直指 functional core 真身（stop_loss 垫片已删）
from trading.compute.stop import should_trigger_stop


def test_grace_period_uses_base_stop():
    """grace 天内 = base_stop（颈线 - stop_atr_mult×ATR，固定）。"""
    # 颈线10, ATR 0.5, stop_atr_mult 2 → base_stop = 10 - 2×0.5 = 9.0
    stop = compute_stop_price(neckline=10.0, atr=0.5, holding_days=2,
                              stop_atr_mult=2.0, grace=5, step=0.1, floor=0.5)
    assert abs(stop - 9.0) < 1e-9


def test_after_grace_tightens_step_atr():
    """grace 天后每日收紧 step×ATR。holding_days=7, grace=5 → 收紧 (7-5)×0.1=0.2 mult。
    eff_mult = 2 - 0.2 = 1.8 → stop = 10 - 1.8×0.5 = 9.1"""
    stop = compute_stop_price(neckline=10.0, atr=0.5, holding_days=7,
                              stop_atr_mult=2.0, grace=5, step=0.1, floor=0.5)
    assert abs(stop - 9.1) < 1e-9


def test_floor_caps_tightening():
    """收紧不低于 floor。step 大到 eff_mult < floor 时卡 floor。
    holding_days=20, grace=5 → 收紧 15×0.5=7.5 → eff_mult=2-7.5=-5.5 → max(-5.5,0.5)=0.5
    stop = 10 - 0.5×0.5 = 9.75"""
    stop = compute_stop_price(neckline=10.0, atr=0.5, holding_days=20,
                              stop_atr_mult=2.0, grace=5, step=0.5, floor=0.5)
    assert abs(stop - 9.75) < 1e-9


def test_grace_zero_degrades_fixed():
    """grace=0/step=0 退化为固定止损（=base_stop）。"""
    stop = compute_stop_price(neckline=10.0, atr=0.5, holding_days=99,
                              stop_atr_mult=2.0, grace=0, step=0.1, floor=0.5)
    assert abs(stop - 9.0) < 1e-9


# ============================================================================
# should_trigger_stop 单测（Layer2 阶段5 · 从 stop_loss_monitor 四缠拆出的纯判定）
# ============================================================================
def test_should_trigger_stop_below_threshold():
    """现价 < 止损价 → 触发（<= 语义，跌破即平仓）。"""
    assert should_trigger_stop(price=9.49, stop_price=9.50) is True


def test_should_trigger_stop_at_threshold():
    """现价 == 止损价 → 触发（<= 非 <，阈值线上下穿越一律视为触发防状态机悬挂）。"""
    assert should_trigger_stop(price=9.50, stop_price=9.50) is True


def test_should_trigger_stop_above_threshold():
    """现价 > 止损价 → 不触发（仍在止损线之上，继续持有）。"""
    assert should_trigger_stop(price=9.51, stop_price=9.50) is False


# ============================================================================
# trading_days_between（plan Task 6 抽 · Task 9 trailing holding_days / Task 8 max_holding 复用）
# ============================================================================
def test_trading_days_between_uses_trade_calendar(monkeypatch):
    """trading_days_between：用 calendar.fetch_trade_cal 算 (start,end] 交易日数（跳周末）。

    物理意图（plan Task 9 · spec §5.3）：
        trailing 的 holding_days 必须【交易日】口径（颈线法 grace/step 按交易日离散），
        自然日会把周末算进去致窗口虚长。本函数从 calendar.fetch_trade_cal 取交易日集，
        数 (start, end] 区间内的交易日（不含 start，含 end——end 是「今日已到」判断日）。
    """
    from trading.compute import stop
    import trading.calendar as cal
    # 构造 2099-01-04(周一) ~ 01-08(周五) 5 个交易日（跳过周末 01-09/10）
    fake_trade_days = {"2099-01-04", "2099-01-05", "2099-01-06", "2099-01-07", "2099-01-08"}
    monkeypatch.setattr(cal, "fetch_trade_cal", lambda year: fake_trade_days)
    # (01-04, 01-08] = 01-05/06/07/08 = 4 个交易日
    assert stop.trading_days_between("2099-01-04", "2099-01-08") == 4
    # 同日 → 0（start 当日不算超期，给足机会）
    assert stop.trading_days_between("2099-01-04", "2099-01-04") == 0
    # end < start → 0
    assert stop.trading_days_between("2099-01-08", "2099-01-04") == 0


def test_trading_days_between_missing_start_returns_zero():
    """start/end 缺失或格式错 → 0（保守视窗口内，向后兼容老 plan 无 entry_date）。"""
    from trading.compute import stop
    assert stop.trading_days_between("", "2099-01-08") == 0
    assert stop.trading_days_between("bad-format", "2099-01-08") == 0
    assert stop.trading_days_between("2099-01-01", "") == 0
