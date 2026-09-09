# -*- coding: utf-8 -*-
"""R6 扩池涨跌停成交建模测试（2026-09-08）。

三态守护：
  ① 默认关（price_limit_model=False / symbol 缺省）= 零行为变化（与关前逐位同）；
  ② 一字涨停日买单排队不可达 → 跳过该日，次日正常价成交；
  ③ 封跌停止损卖出顺延 → 打开日按 min(触发日 stop, 开盘) 记，limit_deferred=True。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategies.neckline.backtest import (EXEC_DEFAULTS, simulate_exit)

DATES = pd.bdate_range("2025-01-01", periods=40)


def make_df(closes, highs=None, lows=None, opens=None):
    closes = list(closes)
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    opens = opens or [c * 0.998 for c in closes]
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": [1e6] * n, "amount": [1e8] * n},
                        index=DATES[:n])


# 形态几何：neckline=10, bottom=8 → H=2；stop=10-1.5×atr；挂单价=10+2.5×atr
# 用 exec 覆写让价位可控（buy_limit_atr_mult=0.15 → 挂 10.15/atr=1.0）。
EXEC = {**EXEC_DEFAULTS, "max_wait": 8, "max_holding": 12,
        "buy_limit_atr_mult": 0.15, "tp1_h_mult": 1.0, "tp_h_mult": 2.0,
        "tp1_portion": 0.5, "stop_atr_mult": 1.5}
IDC = {"stop_atr_mult": 1.5, "tp_h_mult": 2.0}


def _run(df, plm, symbol="600000.SH"):
    return simulate_exit(df, 0, 10.0, 8.0, 1.0, exec={**EXEC, "price_limit_model": plm},
                         id_cfg=IDC, symbol=symbol)


def test_default_off_zero_change():
    """① 默认关：一字涨停日买入照旧成交（与关前行为逐位同）。"""
    # 日 1 一字涨停（high==low=10.99=close，prev=10）→ low 10.99 > 挂单 10.15 不触发；
    # 构造挂单价可触发的一字涨停：close=10.2×… 直接让一字价=10.15 内：prev=9.2
    # 涨停 10%=10.12 < 10.15 挂单价 → 触发。开盘一字。
    closes = [9.2] + [10.12] * 3 + [10.0, 9.9, 9.8, 9.7, 9.6, 9.5]
    df = make_df(closes, highs=[9.2] + [10.12] * 3 + [10.0, 9.9, 9.8, 9.7, 9.6, 9.5],
                 lows=[9.2] + [10.12] * 3 + [9.8, 9.7, 9.6, 9.5, 9.4, 9.3])
    off = _run(df, False)
    # 关=旧行为：一字日成交，entry=min(挂单 10.15, open=10.12×0.998)=10.0998
    assert off["buy_date"] == DATES[1].date()
    assert off["entry"] == pytest.approx(10.12 * 0.998, abs=2e-3)
    assert not off.get("limit_deferred")


def test_one_word_limit_up_buy_skipped():
    """② 开启：一字涨停日（low=10.12≤挂单 10.15 触发）买队列不可达 → 跳到次日。"""
    closes = [9.2, 10.12, 9.95, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2]
    df = make_df(closes, highs=[9.2, 10.12, 10.0, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2],
                 lows=[9.2, 10.12, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2, 9.1, 9.0])
    on = _run(df, True)
    # 一字涨停日跳过 → 次日成交，entry=min(10.15, 次日 open=9.95×0.998)
    assert on["buy_date"] == DATES[2].date()
    assert on["entry"] == pytest.approx(9.95 * 0.998, abs=2e-3)
    off = _run(df, False)
    assert off["buy_date"] == DATES[1].date()   # 关=旧：一字日成交


def test_sealed_limit_down_stop_deferred():
    """③ 开启：止损触发日封死跌停 → 顺延到打开日按 min(stop, open) 记。"""
    # idx1 正常回踩成交（low 9.8≤10.15，entry=min(10.15, 9.95×0.998)=9.93）——
    # 关/开同买入日，隔离封板顺延的单变量效应。
    closes = [9.2, 9.6, 9.6, 8.50, 7.9, 8.4, 8.6, 8.8, 9.0]
    df = make_df(closes,
                 highs=[9.2, 9.65, 9.65, 8.50, 8.1, 8.6, 8.8, 9.0, 9.1],
                 lows=[9.2, 9.4, 9.4, 8.50, 7.5, 8.1, 8.3, 8.5, 8.7],
                 opens=[9.2, 9.95, 9.55, 8.60, 7.6, 8.2, 8.4, 8.6, 8.8])
    # idx3 一字跌停（close=low=8.50≤prev 9.6×0.9×1.001）且 low=8.50≤stop 8.5 触发。
    on = _run(df, True)
    assert on["exit_reason"] == "stop_loss"
    assert on["limit_deferred"] is True
    entry = on["entry"]
    assert entry == pytest.approx(9.95, abs=2e-3)   # idx1 显式 open=9.95<挂单价 10.15
    # 顺延到 idx4（打开日，open 7.6）：fill=min(8.5, 7.6)=7.6
    assert on["lot1_pnl_pct"] == pytest.approx((7.6 - entry) / entry * 100, abs=0.5)
    off = _run(df, False)
    # 关=旧行为：idx3 触发即按 min(8.5, open 8.60)=8.5 记
    assert off["exit_reason"] == "stop_loss"
    assert not off.get("limit_deferred")
    assert off["exit_date"] == DATES[3].date()
    assert off["lot1_pnl_pct"] == pytest.approx((8.5 - entry) / entry * 100, abs=0.5)


def test_symbol_missing_disables_model():
    """symbol 未透传（老调用方）→ 即使 exec 开了也视为关（防静默半开）。"""
    closes = [9.2, 10.12, 9.95, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3]
    df = make_df(closes, highs=[9.2, 10.12, 10.0, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3],
                 lows=[9.2, 10.12, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2, 9.1])
    sim = simulate_exit(df, 0, 10.0, 8.0, 1.0,
                        exec={**EXEC, "price_limit_model": True}, id_cfg=IDC)
    assert sim["buy_date"] == DATES[1].date()   # 无 symbol=旧行为（一字日成交）
