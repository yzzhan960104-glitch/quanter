# -*- coding: utf-8 -*-
"""因果 ZigZag 测试：pivot 标记 + 末尾未确认丢弃 + 未来函数回归。

物理意图：本测试集是蔡森/量化风控红线（无未来函数）的硬证明。
- test_synthetic_w_shape_pivots：合成 W 形（谷-峰-谷-峰）应识别 ≥4 pivot；
- test_last_unconfirmed_pivot_dropped：末尾刚创新极值但后续 K 线不足
  confirm_bars → 滞后确认机制将其丢弃（标 0），切断未来函数；
- test_no_lookahead_bias：对序列 S 识别 pivot 后，末尾追加新数据，
  重叠区间 pivot 标记必须完全一致——这是"算法不偷看未来"的形式化证明。
- test_no_lookahead_bias_reversal_extension：在 test_no_lookahead_bias 基础上，
  追加**反向急跌**序列使 extended 末值与 base 末值差异巨大，专门暴露
  "thresh 价格基准用末值"这一隐式未来函数（见 docstring 警告）。

检测力设计要点（Task3 review Important 修复）：
  原 test_no_lookahead_bias 检测力不足有二：
  (a) 参数 _atr_const(1.0) + 价格 7-13 + max(0.005,...) 下限 → thresh 恰落低敏区间，
      "首根基准 vs 末值基准"差异被抹平，抓不到 thresh 基准类未来函数；
  (b) extended 仅同向追加 [9,8,7]，重叠区每根 i 在 base/ext 中"偷看到的未来"几乎相同，
      连明确的未来函数 bug 都抓不到。
  本版调整参数使 thresh 落非下限临界区间（5%），并新增反向追加用例让 ext 末值剧变
  （100 → 30），使 thresh 基准类未来函数在重叠区显形。
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

    参数选择（Task3 review Important 修复）：
      价格 100 起 + atr 1.0 + zigzag_threshold_atr=5.0
      → thresh = max(0.005, (1.0/100)*5.0) = 0.05 = 5%
      thresh 明显 > 0.005 下限且落非下限临界区间（反转幅度 12% 触发、4% 不触发），
      使"首根基准 vs 末值基准"差异可被检测（若误用末值，追加数据 → thresh 变 → 历史漂移）。

    断言策略：
      base 内的所有 pivot 都距末尾 ≥ confirm_bars（已确认，见 causal_pivots docstring
      关键不变量），在 extended 中距离只会增大、标记永不改变；
      仅比较已确认区段 [0 : len(base) - confirm_bars]，末尾 confirm_bars 根允许因
      "新信息确认"而变化（合法，非未来函数）。
    """
    # base：W 形多反转（12% 大反转必触发 5% thresh；idx 3-4 的 4% 小回调不触发），
    # 末尾 idx 11-13 上行确认 idx 10 最后一个 pivot。
    base = pd.Series(
        [100, 112, 100, 108, 104, 100, 112, 100, 112, 100, 112, 100, 112, 100],
        dtype=float,
    )
    # extended：末尾追加 3 根继续同向的 K 线（不引入新反转 → 不在重叠区产生新 pivot）
    extended = pd.concat([base, pd.Series([105, 110, 115], dtype=float)])
    cfg = StrategyConfig(zigzag_threshold_atr=5.0, confirm_bars=3)
    piv_base = causal_pivots(base, _atr_const(len(base), 1.0), cfg)
    piv_ext = causal_pivots(extended, _atr_const(len(extended), 1.0), cfg)

    # 已确认区段：[0 : len(base) - confirm_bars]，base 内距末尾 ≥ confirm_bars 的 pivot。
    # 这些 pivot 已被 base 内后续 K 线因果确认，追加数据下标记必须永不改变（无历史漂移）。
    confirmed_len = len(base) - cfg.confirm_bars
    np.testing.assert_array_equal(
        piv_base.values[:confirmed_len],
        piv_ext.values[:confirmed_len],
        err_msg="已确认区段 pivot 在追加数据下发生漂移 → 存在未来函数",
    )


def test_no_lookahead_bias_reversal_extension():
    """未来函数回归（反向追加增强版）：在 base 末尾追加**反向急跌**序列，
    使 extended 末值与 base 末值差异巨大（100 → 30），专门暴露
    "thresh 价格基准用末值"这一隐式未来函数。

    物理意图与检测力证明：
      若 thresh 基准误用末值 close.iloc[-1]（而非因果的首根 close.iloc[0]），
      则 base（末值 100，thresh=5%）与 extended（末值 30，thresh=16.7%）的
      thresh 会显著不同 → 重叠区的 12% 反转在 base 中触发但在 extended 中不再触发
      → 已确认区段 pivot 全消失 → 断言失败，抓到未来函数。
      反之，因果实现用首根基准，base/extended 的 thresh 恒为 5%（首根 100 不变），
      已确认区段 pivot 逐点完全一致。

    元验证（见 commit 信息与下方注释）：在 causal_pivots 内临时注入"末值基准"bug，
    本测试**失败**；还原后本测试**通过**——证明红线测试具备检测力。
    """
    # base：与 test_no_lookahead_bias 相同的多反转结构
    base = pd.Series(
        [100, 112, 100, 108, 104, 100, 112, 100, 112, 100, 112, 100, 112, 100],
        dtype=float,
    )
    # extended：末尾追加**反向急跌**反转序列（100 → 30，跌幅 70%）
    # 物理意图：让 extended 末值剧变，使"末值基准 thresh"从 5% 跳到 16.7%，
    # 若存在 thresh 基准类未来函数，重叠区 12% 反转的 pivot 标记会全部消失。
    extended = pd.concat([base, pd.Series([90, 75, 55, 30], dtype=float)])
    cfg = StrategyConfig(zigzag_threshold_atr=5.0, confirm_bars=3)
    piv_base = causal_pivots(base, _atr_const(len(base), 1.0), cfg)
    piv_ext = causal_pivots(extended, _atr_const(len(extended), 1.0), cfg)

    # 已确认区段 pivot 必须逐点完全一致——反向追加下"末值基准"未来函数会在此显形。
    confirmed_len = len(base) - cfg.confirm_bars
    np.testing.assert_array_equal(
        piv_base.values[:confirmed_len],
        piv_ext.values[:confirmed_len],
        err_msg=(
            "反向追加下已确认区段 pivot 漂移 → 存在 thresh 基准类未来函数 "
            "(thresh 应固定用首根 close.iloc[0]，绝不能用末值)"
        ),
    )
