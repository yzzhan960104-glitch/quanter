# -*- coding: utf-8 -*-
"""signal_runner 归因 + 权重：PlannedOrder 带 experiment_id，budget 按 weight 分配。

背景（Task 5）：
- 实验系统配置中心（experiment/ Task 1-4）已上线，现在把实验归因透传到 T-1 信号→订单转换层。
- Task 7 的 `_eod` 会在生成 signal dict 时注入 `experiment_id` / `experiment_weight`。
- 本模块验证：归因字段落到 PlannedOrder；资金额度按各自权重分配；老 signal（无归因）向后兼容。
"""
import pytest

from trading.signal_runner import PlannedOrder, build_orders_from_signals


def _signal(symbol="000001.SZ", entry=10.0, neckline=10.5, bottom=9.5,
            exp_id="e1", weight=0.2):
    """构造带实验归因的颈线法信号 dict。"""
    return {"symbol": symbol, "entry_price": entry, "neckline": neckline,
            "bottom": bottom, "experiment_id": exp_id, "experiment_weight": weight}


def test_planned_order_carries_experiment_id():
    """PlannedOrder 含 experiment_id + experiment_weight 归因字段。"""
    orders = build_orders_from_signals(
        [_signal(exp_id="neckline_v6", weight=0.3)],
        capital=1_000_000, pos_cap=0.05,
        atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert len(orders) == 1
    assert orders[0].experiment_id == "neckline_v6"
    assert orders[0].experiment_weight == 0.3


def test_budget_scaled_by_weight():
    """qty = weight × capital × pos_cap / entry，向下取整 100 股。"""
    full = build_orders_from_signals([_signal(weight=1.0)], capital=1_000_000,
        pos_cap=0.05, atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    half = build_orders_from_signals([_signal(weight=0.5)], capital=1_000_000,
        pos_cap=0.05, atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert full[0].order.qty > half[0].order.qty
    assert full[0].order.qty % 100 == 0   # A 股 100 整手


def test_signal_without_attribution_defaults_weight_one():
    """老 signal（无 experiment_weight）默认 weight=1.0，experiment_id=""（向后兼容）。"""
    s = {"symbol": "000001.SZ", "entry_price": 10.0, "neckline": 10.5, "bottom": 9.5}
    orders = build_orders_from_signals([s], capital=1_000_000, pos_cap=0.05,
        atr_map={"000001.SZ": 0.5}, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0})
    assert orders[0].experiment_weight == 1.0 and orders[0].experiment_id == ""


# ---------------------------------------------------------------------------
# C2 · final-fix：多实验同标的 atr_map 覆盖 → 各 PlannedOrder 必须用各自 signal atr
# ---------------------------------------------------------------------------
def test_per_experiment_atr_no_collision():
    """两条 signal 同标的不同 atr，atr_map 只留最后写入——各 PlannedOrder.stop_price
    必须用各自 signal 自身 atr，不能被 atr_map 的覆盖值串到一起。

    物理意图（C2 缺陷）：_eod 内 ``atr_map[sym] = s["atr"]`` 多实验同标的灰度时被
    最后写入的实验覆盖，signal_runner 读共享 atr_map 算 ``stop_price = neckline -
    stop_mult × atr`` → e1 的止损用了 e2 的 ATR，实盘风险参数错配（违反 spec §0
    「参数以不可变快照锁定」）。修复契约：优先用 signal 自身 atr，fallback atr_map。
    """
    # 同标的两 signal，atr 各 0.4 / 0.8；共享 atr_map 模拟 _eod 灰度覆盖
    # （e2 后写入 → atr_map["000001.SZ"] 被覆盖成 0.8，e1 的 atr 在 map 里被淹没）
    s1 = {"symbol": "000001.SZ", "entry_price": 10.0, "neckline": 10.5, "bottom": 9.5,
          "atr": 0.4, "experiment_id": "e1", "experiment_weight": 0.5}
    s2 = {"symbol": "000001.SZ", "entry_price": 10.0, "neckline": 10.5, "bottom": 9.5,
          "atr": 0.8, "experiment_id": "e2", "experiment_weight": 0.5}
    # atr_map 模拟 _eod 写入：s2 后写覆盖 s1（仅留 0.8）
    atr_map = {"000001.SZ": 0.8}

    orders = build_orders_from_signals(
        [s1, s2], capital=1_000_000, pos_cap=0.05,
        atr_map=atr_map, stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0},
    )
    assert len(orders) == 2

    # 修复前：两 order stop_price 都 = 10.5 - 2.0 × 0.8 = 8.9（e1 被 e2 串味）
    # 修复后：e1 = 10.5 - 2.0 × 0.4 = 9.7；e2 = 10.5 - 2.0 × 0.8 = 8.9（各自不串）
    assert orders[0].experiment_id == "e1"
    assert orders[1].experiment_id == "e2"
    assert orders[0].stop_price == pytest.approx(10.5 - 2.0 * 0.4), \
        "e1 stop_price 必须用自身 atr=0.4，不能被 atr_map 的覆盖值串成 0.8（C2 归因错配）"
    assert orders[1].stop_price == pytest.approx(10.5 - 2.0 * 0.8), \
        "e2 stop_price 用自身 atr=0.8"
    # 反向断言：两 stop_price 必须不同（若相同则证明被串）
    assert orders[0].stop_price != orders[1].stop_price, \
        "同标的不同实验的 stop_price 必须不同（C2 归因不串红线）"
