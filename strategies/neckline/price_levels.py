# -*- coding: utf-8 -*-
"""颈线法入场价位三件套单源（CR-2 · 审计 spec A5 + C1 · 2026-08-15）。

回测⇄实盘等价性是本系统头号资产，此函数是其数学单一归宿（CR-2/A5+C1）：
    迁移前同一套价位公式有三份独立实现——``backtest.simulate_exit`` 内联（回测侧）、
    ``trading/compute/plan.build_orders_from_signals`` 内联（实盘侧）、diag 诊断副本。
    任何一份改公式（如止损基准从颈线换谷底、tp 倍数口径变动）漏改另两份，回测与
    实盘即静默脱节——「回测冠军档套实盘」的全部前提作废。本模块把五价位的数学
    收敛为唯一纯函数，三口改调单源，由 tests/trading/test_price_levels_golden.py
    用迁移前旧实现快照字面量逐位钉死（任何价位数值变化都是事故）。

物理真身选址（Why 在 strategies/neckline/ 而非 trading/compute/）：
    分层铁律 2（tests/test_layer_contract.py）禁止 strategies/ import trading——
    回测侧（strategies/neckline/backtest.py）是本函数主消费方之一，真身必须住在
    strategies 层；实盘侧 trading/compute/plan.py 反向引用本模块（trading→strategies
    是既有合法方向，先例：compute/plan import Signal、compute/__init__ re-export
    ExitAction——与 ExitAction 真身住 strategies/neckline/execution.py 完全同型）。

价位语义（全部以颈线 c* 为锚，H = c* − bottom 形态高度、ATR = 信号日波动）：
    buy_limit = c* + buy_limit_atr_mult × ATR   挂单回踩买入限价（仅回测挂单语义用）
    stop      = c* − stop_atr_mult × ATR        初始止损（trailing 演进的固定基准 base_stop）
    tp1       = c* + tp1_h_mult × H             分级止盈一档（None=未配置→全量 tp2）
    tp2       = c* + tp_h_mult × H              止盈（形态对称目标位）
    cancel_on = c* + cancel_thresh_mult × H     挂单等待期撤单阈值（None=不撤单放飞）
"""
from __future__ import annotations

from dataclasses import dataclass

# C1 参数默认收敛（审计 spec C1 · 六层默认一处钉死）：
# 迁移前默认值散落五处——method_v0.DEFAULTS（stop/tp_h=1.0/2.0）、backtest.EXEC_DEFAULTS
# （buy_limit/tp1/cancel=1.0/1.0/1.0）、plan.py 兜底（stop 2.0 幽灵、tp_h 2.0）、
# critical.py env 缺省（1.0/2.0/…）、schema.py（1.0）。plan.py:98 的 2.0 与其余各口
# 1.0 不一致即漂移实证（生产从未生效——eod_plan 恒显式传键，但幽灵默认是事故温床）。
# 三口（id_cfg / exec / stop_cfg）缺参一律回落此常量；与两口配置默认的逐键相等由
# test_price_level_defaults_align_config_sources 钉死，任何一侧改默认必须显式同步。
PRICE_LEVEL_DEFAULTS: dict = {
    "stop_atr_mult": 1.0,       # 对齐识别层 method_v0.DEFAULTS / critical.py env 缺省
    "buy_limit_atr_mult": 1.0,  # 对齐执行层 backtest.EXEC_DEFAULTS（颈线上方 1ATR 挂单）
    "tp1_h_mult": 1.0,          # 对齐 EXEC_DEFAULTS（第一止盈=颈线+1H）
    "tp_h_mult": 2.0,           # 对齐 DEFAULTS / EXEC 口径（第二止盈=颈线+2H）
    "cancel_thresh_mult": 1.0,  # 对齐 EXEC_DEFAULTS（涨到 tp1 价位即撤单防追高）
}


@dataclass(frozen=True)
class PriceLevels:
    """入场价位三件套值对象（frozen——价位是实盘风险参数，算出即锁定不可变，
    与 Signal 同纪律，spec §0「参数以不可变快照锁定」红线）。"""

    buy_limit: float | None
    """回踩买入限价（颈线 + buy_limit_atr_mult×ATR；仅回测挂单语义用。None=未配置
    （不挂限价单，实盘 entry 由 Signal.entry_price 承载）。"""

    stop: float
    """初始止损 = 颈线 − stop_atr_mult×ATR（trailing 演进的固定基准 base_stop：
    持有期 grace 天内不动，之后由 compute_stop_price 按 step/floor 收紧）。"""

    tp1: float | None
    """分级止盈一档 = 颈线 + tp1_h_mult×H（None=未配置→下游退回 tp2 单笔全平，
    向后兼容老 stop_cfg）。"""

    tp2: float
    """止盈 = 颈线 + tp_h_mult×H（形态学对称目标位）。"""

    cancel_on: float | None
    """撤单阈值 = 颈线 + cancel_thresh_mult×H（挂单等待期 high≥此价 → 涨幅已兑现
    撤买单，过滤「猛突破后回踩」陷阱；None=不撤单放飞所有信号）。"""


def compute_price_levels(
    *,
    c_star: float,
    high: float,
    atr: float,
    stop_atr_mult: float,
    buy_limit_atr_mult: float | None,
    tp1_h_mult: float | None,
    tp_h_mult: float,
    cancel_thresh_mult: float | None,
) -> PriceLevels:
    """入场价位三件套计算（纯函数 · 零 I/O · 确定性）。

    回测⇄实盘等价性是本系统头号资产，此函数是其数学单一归宿（CR-2/A5+C1）——
    回测 simulate_exit 与实盘 build_orders_from_signals 对同一 (c*, H, ATR, 五参数)
    必须从这里拿到逐位相同的价位，等价性由 golden 快照钉死。

    Args:
        c_star:  颈线价位 c*（全部价位的锚点）。
        high:    形态高度 H = c* − bottom（tp1/tp2/cancel_on 的标尺）。
        atr:     信号日 ATR（stop/buy_limit 的波动标尺）。
        stop_atr_mult:        止损 ATR 倍数（默认见 PRICE_LEVEL_DEFAULTS）。
        buy_limit_atr_mult:   挂单价 ATR 倍数（None→buy_limit=None，不挂限价单）。
        tp1_h_mult:           一档止盈 H 倍数（None→tp1=None，全量 tp2）。
        tp_h_mult:            二档止盈 H 倍数。
        cancel_thresh_mult:   撤单阈值 H 倍数（None→cancel_on=None，不撤单放飞）。

    Returns:
        PriceLevels（frozen 五字段）。

    运算形状红线：表达式必须保持与迁移前旧式完全相同（c* + mult × atr / c* − mult ×
    atr），不得重排为 c* + (mult × atr) 之外的结合顺序——浮点最后一位 ulp 漂移也会
    被 golden（G4 乱小数档）拦下。
    """
    return PriceLevels(
        buy_limit=(c_star + buy_limit_atr_mult * atr
                   if buy_limit_atr_mult is not None else None),
        stop=c_star - stop_atr_mult * atr,
        tp1=(c_star + tp1_h_mult * high if tp1_h_mult is not None else None),
        tp2=c_star + tp_h_mult * high,
        cancel_on=(c_star + cancel_thresh_mult * high
                   if cancel_thresh_mult is not None else None),
    )
