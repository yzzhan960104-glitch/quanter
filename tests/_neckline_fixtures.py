# -*- coding: utf-8 -*-
"""颈线法测试公共合成数据单源（2026-08-19 测试库精简 W2 收口）。

原 _ohlc 三份逐字重复（neckline_core / neckline_recognition / price_levels_golden）、
_synth_pattern 两份同数据复刻（recognition + p1_fast_path——后者自述「独立复刻避免
跨模块 import 私有 helper」，收口为公共模块后该顾虑不再成立，且消除双份漂移风险：
一份改了另一份没改，等价性/拒绝矩阵测试会静默失真）。
"""
from __future__ import annotations

import pandas as pd


def ohlc(rows, start="2024-01-01"):
    """rows: [(open, high, low, close, volume), ...] → DatetimeIndex DataFrame。

    物理意图：构造确定性 OHLCV，让 simulate_exit 每个分支精确触发。DatetimeIndex
    模拟真实 sym_df（simulate_exit 用 sym_df.index[idx].date() 取信号日/离场日）。
    freq="B"（工作日）避免周末。调用方需自行保证 OHLC 物理一致性
    （high ≥ max(open,close)，low ≤ min(open,close)）。
    """
    dates = pd.date_range(start, periods=len(rows), freq="B")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=dates)


def synth_pattern():
    """合成 20 根可识别颈线形态（颈线=100 / bottom=90 / H=10 / ATR≈3.6）。

    每根显式指定，确保 detect 的 7 个守卫全部通过（见 test_neckline_recognition
    模块 docstring 的设计推演）。返回 [(open, high, low, close, volume), ...]。
    拒绝路径测试基于此单一条件破坏（no_breakout/no_volume/too_deep/few_tops/
    low_suppression 五轴，见 test_detect_reject_* 与 p1 内核等价例）。
      顶部高点：pos5(100) + pos12(101) 两处 local maxima（±ATR 带内聚集）
      底部谷：  pos8(90) + pos15/16(91) 两处 local minima（bottom_set={90,91}）
      压制：    19/20 根 close<100 → suppression=0.95 ≥ 0.6
      突破：    末根 pos19 close=102 > 100；带量 vol=500 ≥ 1.5×vol5=180
    """
    return [
        (91, 93, 90, 91, 100),       # pos0
        (92, 94, 91, 92, 100),       # pos1
        (93, 95, 92, 93, 100),       # pos2
        (94, 96, 93, 94, 100),       # pos3
        (95, 97, 94, 95, 100),       # pos4
        (97, 100, 96, 98, 100),      # pos5  ← top1（local max, high=100）
        (95, 96, 93, 94, 100),       # pos6
        (93, 94, 91, 92, 100),       # pos7
        (91, 93, 90, 91, 100),       # pos8  ← bottom1（local min, low=90=min_price）
        (93, 95, 92, 93, 100),       # pos9
        (95, 97, 94, 95, 100),       # pos10
        (97, 99, 96, 97, 100),       # pos11
        (99, 101, 97, 99, 100),      # pos12 ← top2（local max, high=101）
        (97, 99, 95, 96, 100),       # pos13
        (94, 96, 92, 93, 100),       # pos14
        (92, 94, 91, 92, 100),       # pos15 ← bottom2
        (92, 94, 91, 92, 100),       # pos16 ← bottom2（连续同低，压制期）
        (93, 95, 92, 93, 100),       # pos17
        (96, 98, 95, 97, 100),       # pos18（回升接近颈线）
        (102, 106, 98, 102, 500),    # pos19 ← 突破根（close=102 > 颈线 100，带量 500）
    ]
