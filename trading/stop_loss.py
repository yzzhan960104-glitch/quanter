# -*- coding: utf-8 -*-
"""海龟 trailing 止损离散纯函数（从 scripts/neckline_backtest.simulate_exit 迁出）。

物理意图（与 simulate_exit:122-135 完全同源）：
- grace 天内：用 base_stop（颈线 - stop_atr_mult×ATR，固定，给趋势确认空间）；
- grace 天后：每日收紧 step×ATR（eff_mult 递减），到 floor 卡底（收紧上限）；
- grace=0/step=0：退化为固定止损（=base_stop，兼容旧行为）。

离散化（二期）：盘后对每只持仓调本函数重算【次日】固定止损价；盘中监控用此固定价，
不移动（符合 spec「盘中不调整」）。回测里是逐根 K 线调；实盘改为每日一次。
"""
from __future__ import annotations


def compute_stop_price(
    neckline: float,
    atr: float,
    holding_days: int,
    stop_atr_mult: float,
    grace: int,
    step: float,
    floor: float | None,
) -> float:
    """给定持有天数算当日止损价（颈线基准，trailing 离散）。"""
    base_stop = neckline - stop_atr_mult * atr
    if grace and step and holding_days > grace:
        eff_mult = stop_atr_mult - (holding_days - grace) * step
        if floor is not None:
            eff_mult = max(eff_mult, floor)
        return neckline - eff_mult * atr
    return base_stop
