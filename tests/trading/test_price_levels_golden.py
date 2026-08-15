# -*- coding: utf-8 -*-
"""CR-2 入场价位三件套 golden 等价测试（回测⇄实盘价位数学单源钉死）。

物理意图（Why 本文件存在）：
    回测⇄实盘等价性是本系统自称的头号资产，而入场价位三件套（止损/止盈/撤单阈值/
    挂单价）在迁移前有三份独立实现（backtest.simulate_exit 内联 / plan.build_orders
    内联 / diag 诊断副本），任何一份改公式漏改另两份即静默脱节。CR-2 把数学收进
    ``strategies/neckline/price_levels.compute_price_levels`` 单源（审计 spec A5+C1），
    本测试用【迁移前旧实现跑出的快照字面量】钉死每个价位——迁移后单源对同输入必须
    逐位（bit-exact，非 approx）复现，任何价位数值变化都是事故。

    golden 字面量来源（2026-08-15 迁移前取证，旧公式逐字复刻跑出 repr）：
        G1 默认档   c*=10, bottom=8, atr=0.5, 五参=(1,1,1,2,1)
        G2 非默认档 c*=9.5, bottom=8.5, atr=0.4, 五参=(2,1.5,0.8,2.5,1.2)
        G3 None 分支 tp1_h_mult/cancel_thresh_mult=None（plan 侧旧可达配置）
        G4 乱小数档 c*=33.7, bottom=29.15, atr=1.234567（含浮点尾差，防精度漂移）

    C1（参数默认收敛）：plan.py 旧 ``stop_cfg.get("stop_atr_mult", 2.0)`` 是幽灵默认——
    生产唯一调用方 eod_plan.compute 恒显式传键（_trade_cfg env 缺省已 1.0），2.0 仅
    测试可触达。收敛后三口缺省一律回落 PRICE_LEVEL_DEFAULTS（=1.0），本测试钉死。
"""
import pandas as pd

from strategies.neckline.backtest import EXEC_DEFAULTS, simulate_exit
from strategies.neckline.method_v0 import DEFAULTS
from strategies.neckline.price_levels import (
    PRICE_LEVEL_DEFAULTS,
    PriceLevels,
    compute_price_levels,
)
from strategies.neckline.signal import Signal
from trading.compute.plan import build_orders_from_signals


# ============================================================================
# ① 单源纯数学 golden（快照字面量 == 逐位断言，手算可核）
# ============================================================================
def test_golden_default_mults():
    """G1 默认档：c*=10/bottom=8(H=2)/atr=0.5 × (1,1,1,2,1) → 手算可核的干净数。"""
    lv = compute_price_levels(
        c_star=10.0, high=2.0, atr=0.5,
        stop_atr_mult=1.0, buy_limit_atr_mult=1.0,
        tp1_h_mult=1.0, tp_h_mult=2.0, cancel_thresh_mult=1.0,
    )
    # buy_limit=10+1×0.5=10.5；stop=10−1×0.5=9.5；tp1/cancel=10+1×2=12；tp2=10+2×2=14
    assert (lv.buy_limit, lv.stop, lv.tp1, lv.tp2, lv.cancel_on) == \
        (10.5, 9.5, 12.0, 14.0, 12.0)


def test_golden_non_default_mults():
    """G2 非默认档：五参全偏离默认，钉乘法交换不出错（0.4×1.5 的浮点路径同旧式）。"""
    lv = compute_price_levels(
        c_star=9.5, high=1.0, atr=0.4,
        stop_atr_mult=2.0, buy_limit_atr_mult=1.5,
        tp1_h_mult=0.8, tp_h_mult=2.5, cancel_thresh_mult=1.2,
    )
    # buy_limit=9.5+1.5×0.4=10.1；stop=9.5−2×0.4=8.7；tp1=9.5+0.8=10.3；
    # tp2=9.5+2.5=12.0；cancel=9.5+1.2=10.7（均旧实现 repr 逐位值）
    assert (lv.buy_limit, lv.stop, lv.tp1, lv.tp2, lv.cancel_on) == \
        (10.1, 8.7, 10.3, 12.0, 10.7)


def test_golden_none_branches():
    """G3 None 分支：tp1_h_mult/cancel_thresh_mult=None → tp1/cancel_on=None（plan 侧
    旧可达语义：老 stop_cfg 不配这两键 → 下游退回 tp2 全平 / 不撤单，零回归）。"""
    lv = compute_price_levels(
        c_star=10.0, high=2.0, atr=0.5,
        stop_atr_mult=1.0, buy_limit_atr_mult=1.0,
        tp1_h_mult=None, tp_h_mult=2.0, cancel_thresh_mult=None,
    )
    assert (lv.buy_limit, lv.stop, lv.tp1, lv.tp2, lv.cancel_on) == \
        (10.5, 9.5, None, 14.0, None)


def test_golden_float_precision_pinned():
    """G4 乱小数档：33.7−1.234567 / 33.7+4.55 的浮点尾差逐位钉死（防表达式重排引入
    最后一位 ulp 漂移——单源必须保持与旧式完全相同的运算形状 c*±mult×atr）。"""
    lv = compute_price_levels(
        # high 用与调用方完全相同的表达式（neckline − bottom）构造：33.7−29.15 在
        # float64 下 ≠ 字面量 4.55（差 1 ulp），调用方传入的就是前者——golden 输入
        # 构造必须镜像调用方，否则钉的是另一个数。
        c_star=33.7, high=33.7 - 29.15, atr=1.234567,
        stop_atr_mult=1.0, buy_limit_atr_mult=1.0,
        tp1_h_mult=1.0, tp_h_mult=2.0, cancel_thresh_mult=1.0,
    )
    assert lv.buy_limit == 34.934567
    assert lv.stop == 32.465433000000004
    assert lv.tp1 == 38.25000000000001
    assert lv.tp2 == 42.80000000000001
    assert lv.cancel_on == 38.25000000000001


def test_price_levels_frozen_value_object():
    """PriceLevels 是 frozen 值对象（价位一经算出不可变——止损价是实盘风险参数，
    spec §0「参数以不可变快照锁定」红线，与 Signal 同纪律）。"""
    lv = compute_price_levels(
        c_star=10.0, high=2.0, atr=0.5, stop_atr_mult=1.0, buy_limit_atr_mult=1.0,
        tp1_h_mult=1.0, tp_h_mult=2.0, cancel_thresh_mult=1.0,
    )
    assert isinstance(lv, PriceLevels)
    try:
        lv.stop = 9.0   # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("PriceLevels 应 frozen，赋值必须抛错")


def test_buy_limit_none_when_mult_none():
    """buy_limit_atr_mult=None → buy_limit=None（挂单价仅回测挂单语义用；不配置
    即不挂限价单，实盘 entry 由 Signal.entry_price 承载——字段语义对称于 tp1/cancel）。"""
    lv = compute_price_levels(
        c_star=10.0, high=2.0, atr=0.5,
        stop_atr_mult=1.0, buy_limit_atr_mult=None,
        tp1_h_mult=1.0, tp_h_mult=2.0, cancel_thresh_mult=1.0,
    )
    assert lv.buy_limit is None


# ============================================================================
# ② C1 参数默认收敛：PRICE_LEVEL_DEFAULTS 与既有两口配置默认逐键对齐
# ============================================================================
def test_price_level_defaults_align_config_sources():
    """C1 铁律：单源默认常量必须与识别层 DEFAULTS + 执行层 EXEC_DEFAULTS 逐键相等。

    Why：这六层默认值曾五处各写一份（method_v0.DEFAULTS / EXEC_DEFAULTS / plan.py
    兜底 / critical.py env / schema），plan.py:98 残留 2.0 幽灵即漂移实证。本断言把
    「单源默认 = 两口配置默认」钉死——任何一侧改默认必须显式同步，静默漂移即红。
    """
    assert PRICE_LEVEL_DEFAULTS["stop_atr_mult"] == DEFAULTS["stop_atr_mult"]          # 识别层 1.0
    assert PRICE_LEVEL_DEFAULTS["tp_h_mult"] == DEFAULTS["tp_h_mult"]                  # 识别层 2.0
    assert PRICE_LEVEL_DEFAULTS["buy_limit_atr_mult"] == EXEC_DEFAULTS["buy_limit_atr_mult"]  # 执行层 1.0
    assert PRICE_LEVEL_DEFAULTS["tp1_h_mult"] == EXEC_DEFAULTS["tp1_h_mult"]           # 执行层 1.0
    assert PRICE_LEVEL_DEFAULTS["cancel_thresh_mult"] == EXEC_DEFAULTS["cancel_thresh_mult"]  # 执行层 1.0


def test_plan_stop_cfg_missing_stop_atr_mult_falls_back_to_1_0():
    """C1 修复钉死：stop_cfg 缺 stop_atr_mult → 兜底 PRICE_LEVEL_DEFAULTS=1.0。

    旧实现兜底 2.0（幽灵默认）：生产唯一调用方 eod_plan.compute 恒显式传键，2.0 仅
    测试可触达、从未在生产生效——故收敛到 1.0 不改变任何生产数值，只消灭漂移源。
    旧值取证：同输入 stop=9.0（10−2×0.5）；新值 9.5（10−1×0.5）。
    """
    sig = Signal(symbol="600000.SH", entry_price=10.0, neckline=10.0, bottom=8.0, atr=0.5)
    orders = build_orders_from_signals(
        [sig], capital=1_000_000.0, pos_cap=0.05,
        atr_map={"600000.SH": 0.5}, stop_cfg={"tp_h_mult": 2.0},   # 故意缺 stop_atr_mult
    )
    assert orders[0].stop_price == 10.0 - PRICE_LEVEL_DEFAULTS["stop_atr_mult"] * 0.5
    assert orders[0].stop_price == 9.5    # 旧 2.0 兜底会得 9.0——本行钉死 C1 修复
    # tp_h_mult 缺省回落 2.0（与旧值一致，非事故）
    assert orders[0].take_profit == 14.0


# ============================================================================
# ③ 行为级等价：backtest 路径 / plan 路径 与单源同输入同价位
# ============================================================================
def _ohlc(rows, start="2024-01-01"):
    """合成 OHLCV（与 tests/test_neckline_core._ohlc 同构，DatetimeIndex 模拟 sym_df）。"""
    dates = pd.date_range(start, periods=len(rows), freq="B")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=dates)


def test_backtest_simulate_exit_matches_single_source():
    """backtest.simulate_exit 产出的 entry/tp1/tp2 与单源逐位一致（默认档 c*=100
    场景与 tests/test_neckline_core 基准同源：entry=102/tp1=110/tp2=120/risk=3.92）。"""
    c_star, bottom, atr = 100.0, 90.0, 2.0
    df = _ohlc([
        (100, 101, 99, 100.5, 1000),   # bar0 信号日（位置锚）
        (102, 103, 102, 102.5, 1000),  # bar1 low=102≤buy_limit=102 成交 entry=102
        (119, 120, 118, 119.5, 1000),  # bar2 high=120≥tp2=120 → tp2 全平
    ])
    sim = simulate_exit(df, 0, c_star, bottom, atr)
    lv = compute_price_levels(
        c_star=c_star, high=c_star - bottom, atr=atr,
        stop_atr_mult=DEFAULTS["stop_atr_mult"],
        buy_limit_atr_mult=EXEC_DEFAULTS["buy_limit_atr_mult"],
        tp1_h_mult=EXEC_DEFAULTS["tp1_h_mult"],
        tp_h_mult=DEFAULTS["tp_h_mult"],
        cancel_thresh_mult=EXEC_DEFAULTS["cancel_thresh_mult"],
    )
    # 成交价=挂单价（open>buy_limit 盘中回踩）→ entry 即 buy_limit；tp1/tp2 落盘 round 3
    assert sim["entry"] == lv.buy_limit == 102.0
    assert sim["tp1"] == round(lv.tp1, 3) == 110.0
    assert sim["tp2"] == round(lv.tp2, 3) == 120.0
    # risk_pct 由 base_stop 反算：(entry−stop)/entry×100 → 单源 stop 代入必须复现
    assert sim["risk_pct"] == round((lv.buy_limit - lv.stop) / lv.buy_limit * 100, 2) == 3.92
    assert sim["exit_reason"] == "tp2"


def test_backtest_cancel_on_matches_single_source():
    """撤单阈值行为等价：等待期 high=单源 cancel_on+1 → skip_target_met（撤单优先）。"""
    c_star, bottom, atr = 100.0, 90.0, 2.0
    lv = compute_price_levels(
        c_star=c_star, high=c_star - bottom, atr=atr,
        stop_atr_mult=DEFAULTS["stop_atr_mult"],
        buy_limit_atr_mult=EXEC_DEFAULTS["buy_limit_atr_mult"],
        tp1_h_mult=EXEC_DEFAULTS["tp1_h_mult"],
        tp_h_mult=DEFAULTS["tp_h_mult"],
        cancel_thresh_mult=EXEC_DEFAULTS["cancel_thresh_mult"],
    )
    df = _ohlc([
        (100, 101, 99, 100.5, 1000),
        # high=111≥cancel_on=110 且 low=104>buy_limit=102（不同日双触 → same_day_both=False）
        (105, lv.cancel_on + 1, 104, 110.5, 1000),
    ])
    sim = simulate_exit(df, 0, c_star, bottom, atr)
    assert sim["exit_reason"] == "skip_target_met"
    assert sim["same_day_both"] is False


def test_plan_build_orders_matches_single_source():
    """plan.build_orders_from_signals 产出的 stop/tp2/tp1/cancel_on 与单源同输入
    逐位一致（实盘侧等价链：G1 同参 → 9.5/14.0/12.0/12.0）。"""
    sig = Signal(symbol="600000.SH", entry_price=10.0, neckline=10.0, bottom=8.0, atr=0.5)
    stop_cfg = {"stop_atr_mult": 1.0, "tp_h_mult": 2.0, "tp1_h_mult": 1.0,
                "tp1_portion": 0.5, "cancel_thresh_mult": 1.0}
    orders = build_orders_from_signals(
        [sig], capital=1_000_000.0, pos_cap=0.05,
        atr_map={"600000.SH": 0.5}, stop_cfg=stop_cfg,
    )
    assert len(orders) == 1
    o = orders[0]
    lv = compute_price_levels(
        c_star=10.0, high=2.0, atr=0.5,
        stop_atr_mult=stop_cfg["stop_atr_mult"],
        buy_limit_atr_mult=stop_cfg.get("buy_limit_atr_mult", PRICE_LEVEL_DEFAULTS["buy_limit_atr_mult"]),
        tp1_h_mult=stop_cfg["tp1_h_mult"], tp_h_mult=stop_cfg["tp_h_mult"],
        cancel_thresh_mult=stop_cfg["cancel_thresh_mult"],
    )
    assert o.stop_price == lv.stop == 9.5
    assert o.take_profit == lv.tp2 == 14.0
    assert o.tp1 == lv.tp1 == 12.0
    assert o.cancel_on == lv.cancel_on == 12.0
