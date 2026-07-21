# -*- coding: utf-8 -*-
"""颈线法信号 → 下单计划转换（Task 3，Task 5 扩展实验归因 + 资金权重）。

把 NecklineMethodStrategy.scan_at 返回的 trade dict 转成 PlannedOrder（OrderRequest + 止损/止盈价）。
仓位：capital × pos_cap × experiment_weight / entry_price，向下取整到 100 整手（A 股）。
止损/止盈：颈线基准 + ATR/H（与 simulate_exit 同口径）。

实验归因（Task 5）：
- PlannedOrder 新增 experiment_id / experiment_weight 两个默认值字段，把实验系统的
  “这个信号属于哪个实验版本、应当分配多少资金权重”的归因信息从 _eod 透传到下单层，
  便于 Task 8 report 按实验聚合 PnL，以及灰度阶段同时跑多版本但各自只占一部分仓位。
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.execution_gateway import OrderRequest


@dataclass
class PlannedOrder:
    """计划单（OrderRequest + 出场价 + 实验归因）。

    新增 experiment_id/experiment_weight 两个默认值字段：
    - 默认值保证老调用点（不带归因）零回归；
    - 实验系统启用后，Task 7 的 _eod 会在 signal dict 里注入，此处原样落盘。
    """
    order: OrderRequest
    stop_price: float
    take_profit: float
    neckline: float
    experiment_id: str = ""          # 归因：所属实验版本（_eod 注入，Task 8 report 按此聚合）
    experiment_weight: float = 1.0   # 归因：落盘时冻结的资金权重（灰度分流，1.0=满仓口径）


def build_orders_from_signals(
    signals: list[dict],
    *,
    capital: float,
    pos_cap: float,
    atr_map: dict[str, float],
    stop_cfg: dict,
) -> list[PlannedOrder]:
    """信号列表 → PlannedOrder 列表。缺 ATR/数据异常的跳过（不抛）。

    资金分配（Task 5）：budget = capital × pos_cap × experiment_weight
    - 每个信号按各自 experiment_weight 分流资金额度（灰度权重在此落地为实际手数）；
    - 老信号无 experiment_weight → 默认 1.0，budget 与原口径完全一致（向后兼容）。

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
        # 实验归因：取每信号各自的权重（灰度分流）；缺省 1.0 = 满仓口径，向后兼容
        weight = s.get("experiment_weight", 1.0)
        # 仓位：capital × pos_cap × weight / entry，向下取整到 100 整手（A 股 1 手=100 股）
        # Why weight 放进 budget：让小权重实验也按整手规则下沉，避免 0~100 股的零股废单；
        # 同权重跨标的资金额度可直接比较，便于回测/实盘对齐归因口径。
        budget = capital * pos_cap * weight
        qty = int(budget / float(entry) / 100) * 100   # 向下取整 100 整手
        if qty <= 0:
            # 资金不足 1 手 → 放弃，防零股废单（小权重灰度时常见，非异常）
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
            # 归因透传：experiment_id 缺省 ""（老链路），experiment_weight 已在 budget 落地
            experiment_id=s.get("experiment_id", ""),
            experiment_weight=weight,
        ))
    return out
