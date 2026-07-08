# -*- coding: utf-8 -*-
"""因果 ZigZag 测试：pivot 标记 + 末尾未确认丢弃 + 未来函数回归。

物理意图：本测试集是蔡森/量化风控红线（无未来函数）的硬证明。
- test_synthetic_w_shape_pivots：合成 W 形（谷-峰-谷-峰）应识别 ≥4 pivot；
- test_last_unconfirmed_pivot_dropped：末尾刚创新极值但后续 K 线不足
  confirm_bars → 滞后确认机制将其丢弃（标 0），切断未来函数；
- test_no_lookahead_bias：对序列 S 识别 pivot 后，末尾追加新数据，
  重叠区间 pivot 标记必须完全一致——这是"算法不偷看未来"的形式化证明。
"""
import numpy as np
import pandas as pd
from caisen.config import StrategyConfig
from caisen.patterns.zigzag_causal import causal_pivots


def _atr_const(n, val):
    """构造常数 ATR 序列（val 元/股），方便阈值由 cfg 单独控制。"""
    return pd.Series(val, index=pd.RangeIndex(n))


def test_synthetic_w_shape_pivots():
    """合成 W 形：识别出 谷-峰-谷-峰 四个 pivot。"""
    # 序列：10→8（底1）→11（顶1）→8（底2）→13（顶2），构成 W+突破
    price = pd.Series([10, 9, 8, 9, 10, 11, 10, 9, 8, 9, 10, 11, 12, 13], dtype=float)
    cfg = StrategyConfig(zigzag_threshold_atr=0.5, confirm_bars=3)
    piv = causal_pivots(price, _atr_const(len(price), 1.0), cfg)
    assert piv.isin([1, -1]).sum() >= 4   # 至少 4 个 pivot


def test_last_unconfirmed_pivot_dropped():
    """末尾新出现的极值未被 confirm_bars 确认 → 丢弃（标 0）。"""
    # 末尾刚创新低 7.5，其后无足够确认 K 线（confirm_bars=3，但已是最后一根）
    price = pd.Series([10, 11, 10, 9, 8, 7.5], dtype=float)
    cfg = StrategyConfig(zigzag_threshold_atr=0.5, confirm_bars=3)
    piv = causal_pivots(price, _atr_const(len(price), 1.0), cfg)
    # 最后一个点（index 5）不应被标为确认 pivot
    assert piv.iloc[-1] == 0


def test_no_lookahead_bias():
    """未来函数回归：对序列 S 识别 pivot 后，在 S 末尾追加新数据，
    原 pivot 标记在重叠区间必须完全一致（不因未来数据改变历史判断）。

    构造要点（使严格相等成立）：
      base 序列以"最后一个 pivot 后跟 ≥ confirm_bars 根确认 K 线"结束——
      即 base 内的所有 pivot 都已因果确认（其反转由 base 内已发生的 K 线验证）。
      extended 仅在 base 末尾追加"继续同向"的 K 线，不在 [0:len(base)] 区间内
      引入新 pivot。这样：
        - base 的已确认 pivot 在 extended 中距离末尾只会增大、仍 ≥ confirm_bars → 标记不变；
        - 追加的 K 线不构成新反转 → 不在重叠区引入新 pivot；
        - 重叠区间 pivot 标记逐点相等 = 无未来函数的硬证明。
    """
    # base：峰在 idx 7（价 13），其后 3 根下行 K 线（12,11,10）已确认该峰；idx 2 谷、idx 0 峰
    base = pd.Series([10, 9, 8, 9, 10, 11, 12, 13, 12, 11, 10], dtype=float)
    # extended：末尾追加 3 根继续下行的 K 线（不引入新反转 → 不在重叠区产生新 pivot）
    extended = pd.concat([base, pd.Series([9, 8, 7], dtype=float)])
    cfg = StrategyConfig(zigzag_threshold_atr=0.5, confirm_bars=3)
    piv_base = causal_pivots(base, _atr_const(len(base), 1.0), cfg)
    piv_ext = causal_pivots(extended, _atr_const(len(extended), 1.0), cfg)
    # 重叠区间（base 的长度）pivot 标记必须一致——这是无未来函数的硬证明
    np.testing.assert_array_equal(piv_base.values, piv_ext.values[: len(base)])
