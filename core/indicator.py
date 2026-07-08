# -*- coding: utf-8 -*-
"""通用技术指标（ATR 等）：从 factors 体系剥离的纯计算函数集合。

定位：
    蔡森形态学专精化重构（Phase 1·Task 3）将原 factors/micro_momentum.py 中
    与具体因子体系耦合度低、可被多模块复用的【纯计算函数】剥离到本模块。
    这些函数只依赖 pandas/numpy，不依赖 FactorRegistry/FactorLoader 等因子框架
    设施，属于零副作用的数学工具——供 core/macro 驾驶舱 ATR 端点与后续蔡森
    risk 层（Risk Parity 头寸 / 移动止损线）按需 import 复用。

迁移范围（仅 atr）：
    - atr(df, window=14) 平均真实波幅 —— 保留（宏观驾驶舱 /macro/factors/{symbol}
      ATR 端点 + 蔡森 risk 头寸定权均引用）
    不迁移：breakout_signal / risk_parity_weight 随 factors 体系整体删除
    （当前无保留代码引用，蔡森 risk 层后续如需会重新实现）。

设计纪律（CLAUDE.md 极简原则）：
    - 第一性原理：ATR 用显式向量化实现（high-low 滚动均值），不引入重型黑盒
      量化库，代码像数学公式一样直白紧凑。
    - 边界审查：防除零、防前视、保留 warm-up NaN，杜绝静默伪造数据污染下游。
"""
from __future__ import annotations

import pandas as pd


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """平均真实波幅 ATR（显式向量化实现）。

    ATR = mean((high - low).rolling(window))。
    物理意图：以单位时间内的高低差均值衡量该标的的"原生噪声水平"，
    作为 Risk Parity 头寸的反比分母。这里采用 high-low 近似（1m/5m 微观
    K 线通常 gap 极小，省略传统 TR 的 close 跳空项以保持极简与零幻觉）。

    防除零：滚动均值可能因停牌/涨跌停等场景退化到 0，下游 1/ATR 将爆 Inf。
    故凡 ATR ≤ 1e-9 处统一抬到 1e-9（与 ε 兜底一致），确保头寸数值有限。
    """
    tr = (df["high"] - df["low"]).abs()
    a = tr.rolling(window).mean()
    # 防 Inf 且不掩盖 warm-up NaN：仅对【非 NaN 且 ≤ ε】的值抬到 ε（防 1/ATR 爆 Inf）；
    #   rolling warm-up 期（前 window-1 根）的 NaN 保持 NaN——绝不静默替换成 1e-9，
    #   否则下游会拿到伪 ATR(1e-9) 污染 Risk Parity 头寸与移动止损线（macro 端点
    #   另有 len<window + pd.isna 守卫兜底）。
    return a.mask(a.notna() & (a <= 1e-9), 1e-9)
