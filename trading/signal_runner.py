# -*- coding: utf-8 -*-
"""颈线法信号 → 下单计划转换（Task 3）。

把 NecklineMethodStrategy.scan_at 返回的 trade dict 转成 PlannedOrder（OrderRequest + 止损/止盈价）。
仓位：capital × pos_cap / entry_price，向下取整到 100 整手（A 股）。
止损/止盈：颈线基准 + ATR/H（与 simulate_exit 同口径）。
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.execution_gateway import OrderRequest


@dataclass
class PlannedOrder:
    """计划单（OrderRequest + 出场价）。"""
    order: OrderRequest
    stop_price: float
    take_profit: float
    neckline: float


def build_orders_from_signals(
    signals: list[dict],
    *,
    capital: float,
    pos_cap: float,
    atr_map: dict[str, float],
    stop_cfg: dict,
) -> list[PlannedOrder]:
    """信号列表 → PlannedOrder 列表。缺 ATR/数据异常的跳过（不抛）。

    Why 跳过而非抛错：自动交易引擎在盘后批处理多只标的时，单只缺 ATR/数据异常
    不应中断整批；此处静默跳过，由上层日志记录后人工补救。
    """
    stop_mult = stop_cfg.get("stop_atr_mult", 2.0)
    tp_mult = stop_cfg.get("tp_h_mult", 2.0)
    out: list[PlannedOrder] = []
    for s in signals:
        sym = s.get("symbol")
        entry = s.get("entry_price")
        neckline = s.get("neckline")
        bottom = s.get("bottom")
        # ATR 缺失/NaN 时直接跳过，避免后续数学运算抛 TypeError/ZeroDivision
        atr = atr_map.get(sym) if sym else None
        if not sym or entry is None or neckline is None or bottom is None or atr is None:
            continue
        # 仓位：capital × pos_cap / entry，向下取整到 100 整手（A 股 1 手=100 股）
        budget = capital * pos_cap
        qty = int(budget / float(entry) / 100) * 100   # 向下取整 100 整手
        if qty <= 0:
            # 资金不足 1 手 → 放弃，防零股废单
            continue
        # H = 颈线到底部的高度，作为风险报酬比的标尺
        h = float(neckline) - float(bottom)
        # 止损 = 颈线 - stop_mult × ATR（ATR 口径，过滤窄幅噪音）
        stop_price = float(neckline) - stop_mult * float(atr)
        # 止盈 = 颈线 + tp_mult × H（形态学对称目标位）
        take_profit = float(neckline) + tp_mult * h
        out.append(PlannedOrder(
            order=OrderRequest(symbol=sym, qty=float(qty), side="buy", price=float(entry)),
            stop_price=stop_price, take_profit=take_profit, neckline=float(neckline),
        ))
    return out
