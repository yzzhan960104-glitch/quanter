# -*- coding: utf-8 -*-
"""trading.compute.stop — 止损/止盈/移动止损纯函数集合（functional core）。

物理定位（Layer2 阶段2 · spec §3.5/§4）：
    本模块集中放置所有【纯判定/计算】的止损系列函数，回测与实盘共用同一套判定
    逻辑（杀手不变量）。无 I/O、无状态、确定性，仅依赖标准库。

迁移来源（strangler 红线① · 逻辑零改动）：
    - compute_stop_price（海龟 trailing 离散化）：trading/stop_loss.py
    - check_stop_loss / check_take_profit / update_trailing_stop：trading/order_state.py
      （这些纯函数原嵌在订单状态机模块里——但它们不读写状态，仅是数学判定，归位
      compute 后 order_state 反向 re-export 保持调用点零改动）。

    原模块（trading/stop_loss.py / trading/order_state.py）留垫片 re-export。
"""
from __future__ import annotations


# ============================================================================
# 海龟 trailing 止损离散（从 scripts/neckline_backtest.simulate_exit 迁出）
# ============================================================================
def compute_stop_price(
    neckline: float,
    atr: float,
    holding_days: int,
    stop_atr_mult: float,
    grace: int,
    step: float,
    floor: float | None,
) -> float:
    """给定持有天数算当日止损价（颈线基准，trailing 离散）。

    物理意图（与 simulate_exit:122-135 完全同源）：
    - grace 天内：用 base_stop（颈线 - stop_atr_mult×ATR，固定，给趋势确认空间）；
    - grace 天后：每日收紧 step×ATR（eff_mult 递减），到 floor 卡底（收紧上限）；
    - grace=0/step=0：退化为固定止损（=base_stop，兼容旧行为）。

    离散化（二期）：盘后对每只持仓调本函数重算【次日】固定止损价；盘中监控用此固定价，
    不移动（符合 spec「盘中不调整」）。回测里是逐根 K 线调；实盘改为每日一次。
    """
    base_stop = neckline - stop_atr_mult * atr
    if grace and step and holding_days > grace:
        eff_mult = stop_atr_mult - (holding_days - grace) * step
        if floor is not None:
            eff_mult = max(eff_mult, floor)
        return neckline - eff_mult * atr
    return base_stop


# ============================================================================
# 固定止损/止盈 + ATR 移动止损（原 trading/order_state.py:284-339）
# ============================================================================
def check_stop_loss(entry: float, price: float, pct: float) -> bool:
    """固定止损：当最新价 price ≤ 入场价 entry*(1-pct) 时触发离场。

    参数：
        entry: 开仓均价（成本基准）。
        price: 当前最新成交价（用于判定是否跌穿止损线）。
        pct:   止损百分比，如 0.05 表示跌 5% 即止损。

    返回：
        True 表示已触及/跌穿止损线，应立即平仓。

    边界说明：
        采用 <= 而非 <，确保价格恰好等于止损线时也触发——
        风控宁可「多平一单」也不容忍「阈值附近继续持仓博反弹」。
    """
    return price <= entry * (1.0 - pct)


def check_take_profit(entry: float, price: float, pct: float) -> bool:
    """固定止盈：当最新价 price ≥ 入场价 entry*(1+pct) 时触发离场。

    参数：
        entry: 开仓均价（成本基准）。
        price: 当前最新成交价（用于判定是否涨破止盈线）。
        pct:   止盈百分比，如 0.05 表示涨 5% 即止盈。

    返回：
        True 表示已触及/涨破止盈线，应平仓兑现利润。

    边界说明：
        采用 >= 触发，与 check_stop_loss 的 <= 对称——
        阈值线上下穿越一律视为已达成条件，规避「卡在阈值未成交」的状态机悬挂。
    """
    return price >= entry * (1.0 + pct)


def update_trailing_stop(high: float, atr: float, k: float, prev_stop: float) -> float:
    """ATR 移动止损：依据本轮最高价动态抬升止损线，只上移不下移。

    公式：new_stop = high - atr * k

    参数：
        high:      本观察窗口（如一根 K 线或一次 Tick 聚合）的最高价。
        atr:       当前 ATR（平均真实波幅），用于刻画波动幅度。
        k:         ATR 乘数，决定止损线离高价的「呼吸距离」；k 越大越宽松。
        prev_stop: 上一轮已锁定的止损线（首轮可传 0.0 或极小值）。

    返回：
        更新后的止损价（≥ prev_stop，永不回退）。

    核心约束（只上移不下移）：
        若本轮新高回撤导致 new_stop < prev_stop，说明这只是普通波动而非趋势破坏，
        此时仍沿用 prev_stop——既避免止损线被噪声拉低，又锁住此前浮盈。
        max(new_stop, prev_stop) 一行实现，显式且无状态。
    """
    new_stop = high - atr * k
    return max(new_stop, prev_stop)
