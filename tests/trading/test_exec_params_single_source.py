# -*- coding: utf-8 -*-
"""执行参数单源收敛测试（2026-08-17）：exec_params 快照 > stop_cfg(env) > 内置缺省。

背景（双源分叉实测，trade_id 10110356_301584.SZ_2026-08-17 的 SIGNAL.meta）：
    计划装配侧曾按 _trade_cfg() env 缺省算价位——tp_h_mult 2.0（实验 2.5）、
    tp1_portion 0.5（实验 0.3）、max_wait 5（实验 8）、cancel_thresh_mult 1.0（实验 2.0），
    只有挂单价来自实验。本收敛让 exec_params（detect_signal 抄录实验解析值）优先，
    env/stop_cfg 降为缺键兜底。
"""
from __future__ import annotations

import pytest

from strategies.neckline.signal import Signal, signal_to_dict
from trading.compute.plan import build_orders_from_signals

# 复现实测信号几何（301584.SZ @2026-08-14：neckline 23.89 / bottom 19.19 / atr 1.0534）
NECK, BOTTOM, ATR = 23.89, 19.19, 1.0534
H = NECK - BOTTOM  # 4.70

# ACTIVE 实验 neckline_disc_20260725_25c602 的执行键（实验口径）
EXP_PARAMS = {
    "stop_atr_mult": 1.0, "tp_h_mult": 2.5, "tp1_h_mult": 1.0, "tp1_portion": 0.3,
    "max_wait": 8, "cancel_thresh_mult": 2.0, "max_holding": 20,
    "trailing_grace": 0, "trailing_step": 0.0, "trailing_floor": 0.0,
}

# env 缺省口径（_trade_cfg()，曾实测造成分叉的值）
ENV_STOP_CFG = {
    "stop_atr_mult": 1.0, "tp_h_mult": 2.0, "max_wait": 5,
    "tp1_h_mult": 1.0, "tp1_portion": 0.5, "cancel_thresh_mult": 1.0,
}


def _sig(exec_params=None) -> Signal:
    return Signal(
        symbol="301584.SZ", neckline=NECK, bottom=BOTTOM,
        entry_price=NECK + 0.5 * ATR, atr=ATR,
        formed_at="2026-08-14", exec_params=exec_params,
    )


def _build(sig) -> list:
    out = build_orders_from_signals(
        [sig], capital=1_000_000.0, pos_cap=0.05,
        atr_map={"301584.SZ": ATR}, stop_cfg=dict(ENV_STOP_CFG))
    assert len(out) == 1
    return out[0]


def test_exec_params_override_env_for_price_levels():
    """exec_params（实验口径）优先：价位/比例反算乘数 = 实验值（非 env 缺省）。"""
    po = _build(_sig(EXP_PARAMS))
    # tp2 = 颈线 + 2.5H = 35.64（env 2.0 会算成 33.29——实测分叉值）
    assert po.take_profit == pytest.approx(NECK + 2.5 * H)
    # stop = 颈线 − 1.0×ATR
    assert po.stop_price == pytest.approx(NECK - 1.0 * ATR)
    # tp1 = 颈线 + 1.0H；cancel_on = 颈线 + 2.0H（env 1.0 会算成 28.59——实测分叉值）
    assert po.tp1 == pytest.approx(NECK + 1.0 * H)
    assert po.cancel_on == pytest.approx(NECK + 2.0 * H)
    assert po.tp1_portion == pytest.approx(0.3)
    assert po.max_wait == 8
    # 快照随单透传（SIGNAL.meta 落盘 / 巡检 decide_cfg 消费）
    assert po.exec_params == EXP_PARAMS


def test_legacy_signal_without_exec_params_falls_back_to_stop_cfg():
    """老信号 exec_params=None → 走 stop_cfg(env) 兜底（向后兼容零回归）。"""
    po = _build(_sig(None))
    assert po.take_profit == pytest.approx(NECK + 2.0 * H)   # env 2.0
    assert po.tp1_portion == pytest.approx(0.5)              # env 0.5
    assert po.max_wait == 5                                   # env 5
    assert po.cancel_on == pytest.approx(NECK + 1.0 * H)     # env 1.0
    assert po.exec_params is None


def test_partial_exec_params_fills_from_env():
    """exec_params 缺键（部分覆盖）→ 缺的键走 env 兜底（逐键优先级，非全有全无）。"""
    po = _build(_sig({"tp_h_mult": 2.5}))   # 只覆盖 tp_h_mult
    assert po.take_profit == pytest.approx(NECK + 2.5 * H)   # 实验值
    assert po.tp1_portion == pytest.approx(0.5)              # env 兜底
    assert po.max_wait == 5                                   # env 兜底


def test_signal_to_dict_carries_exec_params():
    """序列化（meta 落盘链）带 exec_params。"""
    d = signal_to_dict(_sig(EXP_PARAMS))
    assert d["exec_params"] == EXP_PARAMS
