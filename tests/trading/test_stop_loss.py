# -*- coding: utf-8 -*-
"""海龟 trailing 止损离散纯函数单测（Task 2）。"""
from trading.stop_loss import compute_stop_price


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
