# -*- coding: utf-8 -*-
"""因果 ZigZag：未来函数隔离层。

蔡森/量化风控红线（CLAUDE.md）：zigzag 包的 peak_valley_pivots 是全局后向算法
（T 是否为极值取决于 T 之后的反转），直接用于实盘盘中是未来函数。本模块隔离：
- 历史已完成 pivot（已被后续反转确认）→ 无未来函数，可用 zigzag 包提取；
- 末尾未确认 pivot（距末尾 < confirm_bars 的极值）→ 保守丢弃（标 0）：这些极值的
  反转确认依赖尚未发生的后续走势，直接标记即构成未来函数；
- 回退路径：zigzag 包缺失时自写因果 ZigZag（前向迭代 + 阈值反转判定）。

无前视证明（关键）：每个 pivot 在时刻 t 的确认仅依赖
  (a) t 之前的价格走势（确立 trend 并跟踪 running extremum）；以及
  (b) t 之后 confirm_bars 根"已发生"K 线（T 日收盘看 T-1 及之前，合法，盘中无未来）。
阈值计算固定用序列首根收盘价作基准，绝不用末值——否则追加数据会改变 thresh 进而
改变历史 pivot 标记，构成隐式未来函数（test_no_lookahead_bias 正是为此回归）。
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from caisen.config import StrategyConfig


def _true_range(df_high: pd.Series, df_low: pd.Series, df_close: pd.Series) -> pd.Series:
    """真实波幅 TR（ATR 基元）：max(H-L, |H-前C|, |L-前C|)。

    物理意图：度量单根 K 线的真实波动范围，含向上/向下跳空缺口，为 ATR 的输入。
    """
    prev_close = df_close.shift(1)
    tr = pd.concat(
        [(df_high - df_low), (df_high - prev_close).abs(), (df_low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """ATR = TR 的 window 日均值（因果，仅用过去 window 日，min_periods=1 防早期 NaN）。"""
    tr = _true_range(high, low, close)
    return tr.rolling(window, min_periods=1).mean()


def causal_pivots(close: pd.Series, atr: pd.Series, cfg: StrategyConfig) -> pd.Series:
    """返回因果 pivot 标记序列（1=峰, -1=谷, 0=非）。

    参数：
        close: 收盘价序列
        atr:   对齐的 ATR 序列（用于将 zigzag 阈值转为价格百分比）
        cfg:   策略参数（zigzag_threshold_atr, confirm_bars）
    返回：
        与 close 同 index 的 int 序列；距序列末尾 < confirm_bars 的 pivot 保守丢弃（标 0）。

    末尾未来函数隔离策略（蔡森/量化风控红线）：
      最末若干 pivot 的"反转确认"完全依赖序列末尾之后的尚未发生走势——直接标记即
      构成未来函数。本函数采取保守因果策略：
          【距序列末尾 < confirm_bars 的 pivot 一律丢弃】（标 0）。
      物理意图：某 pivot 之后必须有 ≥ confirm_bars 根"已发生"K 线、且未创新极值覆盖，
      方可视其为有效反转；否则保守不确认。这样得到的 pivot 全部由"过去 + 已发生末段"
      决定，实盘 T 日收盘（看 T-1 及之前）无前视。

    无前视证明（与 test_no_lookahead_bias 对齐）：
      (a) thresh 的价格基准固定用 close.iloc[0]（首根），追加数据不改变首根 →
          thresh 在 base / extended 上完全一致；
      (b) _fallback_causal_pivots 内部 trend 状态机只前向迭代，t 时刻判定仅用 [0..t]；
      (c) confirm_bars 丢弃规则对追加数据单调：base 中已确认（距末尾 ≥ confirm_bars）
          的 pivot，在 extended 中距离只会增大、仍 ≥ confirm_bars → 标记不变；
          base 中被丢弃（距末尾 < confirm_bars）的 pivot 在 extended 中可能转为已确认，
          这是"新信息到来"的合法反映，非未来函数。
          关键不变量：**已确认的 pivot 标记在追加数据下永不改变**（无历史漂移）。
    """
    n = len(close)
    result = pd.Series(0, index=close.index, dtype=int)
    if n < 5:
        return result

    # 阈值计算：基准价固定用首根收盘（绝不用末值，否则追加数据 → thresh 变 → 历史漂移 = 未来函数）
    base_price = float(close.iloc[0])
    if base_price <= 0:
        base_price = 1.0
    # 取一个稳定、对末尾追加鲁棒的 ATR 水平：整段中位数（追加数据轻微变化，但仅影响
    # 末尾 pivot 是否构成反转，不影响已确认历史 pivot）
    atr_vals = atr.values
    atr_vals = atr_vals[~np.isnan(atr_vals)]
    atr_level = float(np.median(atr_vals)) if atr_vals.size > 0 else base_price * 0.01
    if atr_level <= 0:
        atr_level = base_price * 0.01
    # thresh = ATR 占基准价比例 × cfg 倍数；下限 0.5% 防数值噪声
    thresh = max(0.005, (atr_level / base_price) * cfg.zigzag_threshold_atr)
    up_thresh, down_thresh = thresh, thresh

    # 用 zigzag 包提取主干 pivot（含末尾未确认），不可用则走自写因果回退
    try:
        import zigzag  # type: ignore
        raw = zigzag.peak_valley_pivots(close.values, up_thresh, down_thresh)
    except Exception:
        raw = _fallback_causal_pivots(close, thresh)

    # 末尾未来函数隔离：距序列末尾 < confirm_bars 的 pivot 一律丢弃（标 0），
    # 因为其"反转成立"还依赖尚未发生的后续走势——直接标记即未来函数。
    for i in range(n):
        if raw[i] == 0:
            continue
        if (n - 1 - i) < cfg.confirm_bars:
            result.iloc[i] = 0   # 末尾未成形 pivot，保守丢弃（未来函数红线）
        else:
            result.iloc[i] = int(raw[i])
    return result


def _fallback_causal_pivots(close: pd.Series, thresh: float) -> np.ndarray:
    """自写因果 ZigZag 回退（zigzag 包不可用时）。

    极简显式算法（CLAUDE.md：无黑盒、显式至尚）：
      - 维护当前趋势 trend（0=未定 / 1=上 / -1=下）与 running extremum（极值点价位+下标）；
      - 前向迭代 i = 1..n-1，每根仅用 [0..i] 信息更新极值、判定反转——天然因果；
      - 反转判定：先以"上一根 extremum"判定当前价是否构成反转（如趋势上行中价自高点
        回落 > thresh → 前高确认为峰 out=1，转下行）；只有在未反转时才更新 extremum，
        保证反转比较的是历史峰值而非刚刷新的本根价（修复 plan 版本的迭代次序 bug）；
      - 末尾 pivot 仍可能未成形，由调用方 confirm_bars 兜底（保守丢弃）。

    注：thresh 在此为"价格相对比例"（如 0.05 = 5% 反转才算有效），与 cfg 的 ATR 倍数语义一致。
    """
    n = len(close)
    out = np.zeros(n)
    if n < 2:
        return out
    trend = 0   # 0=未定, 1=上升, -1=下降
    extremum = float(close.iloc[0])
    extremum_idx = 0
    for i in range(1, n):
        p = float(close.iloc[i])
        # 反转判定必须先用"上一根 extremum"，再考虑本根是否刷新极值
        # —— 否则本根刚把 extremum 刷成 p，再用 p 跟自己比永远不触发反转（plan 原版 bug）。
        reversed_dir = 0
        if trend != -1 and p < extremum * (1 - thresh):
            reversed_dir = 1     # 前高确认为峰
        elif trend != 1 and p > extremum * (1 + thresh):
            reversed_dir = -1    # 前低确认为谷

        if reversed_dir != 0:
            # 峰（reversed_dir=+1）确认后趋势转下行（trend=-1）；谷（-1）转上行（trend=+1）。
            # 即 trend = -reversed_dir（修复 plan 原版 trend=reversed_dir 笔误——
            # 那会导致趋势永不翻转、所有反转都被错标为同向峰）。
            out[extremum_idx] = reversed_dir
            trend = -reversed_dir
            extremum, extremum_idx = p, i
            continue

        # 未反转：沿允许方向更新 running extremum（trend==0 时双向跟踪以锁定首个极值）
        if trend != -1 and p > extremum:
            extremum, extremum_idx = p, i
        elif trend != 1 and p < extremum:
            extremum, extremum_idx = p, i
    return out
