# -*- coding: utf-8 -*-
"""信号转下单单测（Task 3）。

Layer2 阶段1：signals 改为 list[Signal]（frozen dataclass），测试构造 Signal 替代 dict。
"""
from strategies.signal import Signal
# Layer2 阶段6 follow-up #4a：signal_runner 垫片已删，改指真身 trading.compute.plan
from trading.compute.plan import build_orders_from_signals, PlannedOrder


def test_build_orders_position_sizing():
    """单标的：capital 100万 × pos_cap 0.05 = 5万，entry 10 元 → 5000 股 → 整手 5000。
    附 stop_price（颈线-stop_mult×atr）+ take_profit（颈线+tp_mult×H）。"""
    signals = [Signal(
        symbol="600000.SH", entry_price=10.0, neckline=9.5, bottom=8.5,
        signal_type="neckline",
    )]
    orders = build_orders_from_signals(
        signals, capital=1_000_000.0, pos_cap=0.05,
        atr_map={"600000.SH": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0},
    )
    assert len(orders) == 1
    o = orders[0]
    assert o.order.symbol == "600000.SH"
    assert o.order.side == "buy"
    assert o.order.qty == 5000                      # 5万/10元=5000，整100手
    assert o.order.price == 10.0
    # 止损 = 颈线9.5 - 2×0.5 = 8.5；止盈 = 颈线9.5 + 2×(9.5-8.5)=11.5
    assert abs(o.stop_price - 8.5) < 1e-9
    assert abs(o.take_profit - 11.5) < 1e-9


def test_build_orders_skip_missing_atr():
    """无 ATR 的标的跳过（防 None 运算）。"""
    signals = [Signal(symbol="X.SH", entry_price=10.0, neckline=9.5, bottom=8.5)]
    orders = build_orders_from_signals(
        signals, capital=1_000_000.0, pos_cap=0.05,
        atr_map={}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert orders == []


# ============================================================================
# Task 7（P0-3 分级止盈）：build_orders 算 tp1 = 颈线 + tp1_h_mult × H
# ============================================================================
def test_build_orders_persist_tp1_tp1_portion():
    """build_orders 从 stop_cfg 读 tp1_h_mult/tp1_portion，算 tp1 = 颈线 + tp1_h_mult × H。

    物理意图（plan Task 7 Step 1-2 · 对齐缺口 P0-3）：
        回测 simulate_exit 用 tp1_h_mult×H 算 tp1 锁利位 + tp1_portion 加权两批止盈，
        实盘 build_orders 原先只算 tp2（take_profit = 颈线 + tp_h_mult×H）无 tp1 字段。
        本测试守 build_orders 从 stop_cfg 读两参数 → PlannedOrder.tp1/tp1_portion 落盘。

    断言：
      - tp1 = 颈线 + tp1_h_mult × H（9.5 + 1.0×(9.5-8.5) = 10.5）；
      - tp1_portion 从 stop_cfg 读（默认 0.5）；
      - tp2（take_profit）不变（向后兼容）。
    """
    signals = [Signal(
        symbol="600000.SH", entry_price=10.0, neckline=9.5, bottom=8.5,
        signal_type="neckline",
    )]
    orders = build_orders_from_signals(
        signals, capital=1_000_000.0, pos_cap=0.05,
        atr_map={"600000.SH": 0.5},
        stop_cfg={
            "stop_atr_mult": 2.0, "tp_h_mult": 2.0,
            "tp1_h_mult": 1.0, "tp1_portion": 0.5,
        },
    )
    assert len(orders) == 1
    o = orders[0]
    # tp1 = 颈线 + tp1_h_mult × H = 9.5 + 1.0 × (9.5 - 8.5) = 10.5
    assert abs(o.tp1 - 10.5) < 1e-9
    assert abs(o.tp1_portion - 0.5) < 1e-9
    # tp2（take_profit）= 颈线 + tp_h_mult × H = 9.5 + 2.0 × 1.0 = 11.5（向后兼容）
    assert abs(o.take_profit - 11.5) < 1e-9


def test_build_orders_tp1_default_portion_zero_when_cfg_missing():
    """stop_cfg 缺 tp1_h_mult/tp1_portion → 默认 None/0.0（_place_take_profit 退回 tp2）。

    物理意图（向后兼容）：
        老 stop_cfg（Task 7 前）只有 stop_atr_mult/tp_h_mult，build_orders 不应崩；
        tp1_portion=0 时 _place_take_profit 走 sanity 分支只挂 tp2 全量，零回归。
    """
    signals = [Signal(
        symbol="600000.SH", entry_price=10.0, neckline=9.5, bottom=8.5,
        signal_type="neckline",
    )]
    orders = build_orders_from_signals(
        signals, capital=1_000_000.0, pos_cap=0.05,
        atr_map={"600000.SH": 0.5},
        stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0},  # 无 tp1_h_mult / tp1_portion
    )
    assert len(orders) == 1
    o = orders[0]
    # tp1 缺省 None（_place_take_profit 检测 falsy 退回 tp2 单笔）
    assert o.tp1 is None
    assert o.tp1_portion == 0.0


def test_scan_live_signal_carries_rr():
    """R3：scan_live 返回的 Signal 携带 rr（实际口径，从 detect res['rr'] 读）。

    物理意图（颈线法算法修复 R3）：Task 1 已让 ``detect_neckline_method`` 返回
    实际口径 rr（基于 stop_price=颈线-N×ATR、take_profit=颈线+N×H 的真实盈亏比，
    不再是写死的 2.0）。本测试守住"scan_live 把 detect 返的 rr 原样透传到 Signal.rr"，
    为后续 PlannedOrder.rr → order_dict["rr"] → 钉钉 md 展示的真实盈亏比链路打头。
    缺此透传 → 研究员人审看到的盈亏比与 detect 实际计算脱节，错把"低盈亏比形态"
    当"达标信号"放行，风险归因失真。

    Fixture：复用 ``tests/test_neckline_recognition._synth_pattern`` 验证过的合成颈线形态
    （颈线=100/bottom=90/H=10/ATR≈3.6，20 根 OHLCV，末根放量突破 close=102>100）。
    brief 给的"全 9/9.5 均匀序列 + 末根突破"无法满足 detect 的 7 个守卫
    （顶部聚集/压制时长/双底等），故改用经过 Task 1 实证的 fixture。
    """
    import sys
    from pathlib import Path
    import pandas as pd
    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from strategies.neckline_method import NecklineMethodStrategy
    # 复用既有已实证的合成颈线形态 fixture（颈线=100 / bottom=90 / 末根 close=102 突破）
    sys.path.insert(0, str(_ROOT / "tests"))
    from test_neckline_recognition import _synth_pattern, _ohlc  # noqa: E402

    df = _ohlc(_synth_pattern())
    # window=20 对齐 fixture 长度；末根 date 转短 ISO 与 scan_live 内部 breakout_date 归一一致
    strat = NecklineMethodStrategy(cfg_override={"window": 20})
    last_date = pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")
    sigs = strat.scan_live("TEST.SZ", df, last_date)
    assert len(sigs) == 1
    # rr 必须透传成功（>0 即实际口径计算出来的，非 None、非 0、非负）
    assert sigs[0].rr is not None and sigs[0].rr > 0
