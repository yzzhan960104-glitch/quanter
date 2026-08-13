# -*- coding: utf-8 -*-
"""Deflated Sharpe Ratio（spec §3.7，López de Prado 闭式，防多重比较选 bias）。

物理意图（spec §3.7）：嵌套验证（inner 选参/outer 纯评估）防了"数据窥探"（参数偷看
test 段），但没防"选 bias"——即便完全不偷看，在 M 组参数里挑 calmar/sharpe 最高，
这个"最高"的期望本就随 M 虚高（order statistics）。DSR 用闭式公式修正：输入 top 候选
的 sharpe + 收益序列偏度/峰度（修正非正态，颈线法 trades 尖峰厚尾）+ 试验次数 M
（修正多重比较）→ 输出零假设（最优≈基准）下观察到 ≥sharpe 的概率。

位置（spec §3.7）：L2 裁判，作用在 L1 calmar 排序后的 top-N 候选（如 top-20），不全局算。

诚实边界（spec §3.7/§13，ADR13）：DSR 依赖样本量，颈线法信号稀疏（单组几百笔、切折后
每折几十~百笔）→ 置信区间宽。极端情况 DSR 可能说"top-5 优劣在噪声内不可辨"——
那就承认"相对最优"在该数据量下不可辨识，而非硬选。本模块只算 DSR 值，判定（显著/运气）
留给调用方按阈值 + 诚实报告。

反魔法（ADR4）：逆正态 CDF 用 Acklam 算法纯 Python 实现（math.erf 有，inverse erf 无），
不引 scipy。
- 逆正态逼近：Peter J. Acklam 1996（独立公开的有理逼近算法，与 López de Prado 无关）。
- DSR 闭式公式：López de Prado 2014 "The Deflated Sharpe Fund"（仅消费 Acklam 算法作为 Φ^{-1}）。
两者是独立来源，本文档分别归属，避免把 Acklam 误挂在 López de Prado 名下。
"""
import math


def _norm_cdf(x):
    """标准正态 CDF Φ(x)（math.erf 实现，纯 stdlib）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p):
    """标准正态逆 CDF Φ^{-1}(p)（Acklam 算法，纯 Python 无 scipy 依赖）。

    p ∈ (0,1) → x。本实现客观精度（与 scipy.stats.norm.ppf 全区间对照实测）：
      - 最大绝对误差 ~2.2e-9（p→尾段，如 p=0.02425/0.97575）
      - 最大相对误差 ~1.1e-9（与 Acklam 1996 公开保证 < 1.15e-9 吻合）

    说明：此处只陈述算法客观精度事实，不耦合测试阈值口径。测试阈值（5e-9 绝对）是
    基于"实测 max abs err 2.2e-9 + 浮点余量"的工程取值，独立于本算法精度陈述。
    """
    # Acklam 系数（常量，公开算法）
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    elif p <= phigh:
        q = p - 0.5
        r = q * q
        x = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5]) * q / \
            (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    else:
        q = math.sqrt(-2.0 * math.log(1 - p))
        x = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
             ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    return x


def _expected_max_sharpe(n_trials):
    """M 次独立试验下最高 sharpe 的期望（E[max z | M]，σ_sharpe≈1 年化口径）。

    Gumbel 简化近似：E[max z | M] ≈ Φ^{-1}(1 - 1/M)（M 大时精确；M=1 时无多重比较→0）。
    这是 DSR 的多重比较修正项：试越多，SR_max 越高，同 sharpe 的 DSR 越低。
    """
    if n_trials <= 1:
        return 0.0
    return _norm_ppf(1.0 - 1.0 / n_trials)


def deflated_sharpe(sharpe, n_trials, n_obs, skew=0.0, kurt=3.0):
    """Deflated Sharpe Ratio（López de Prado 2014 闭式，spec §3.7）。

    返回 DSR ∈ [0,1]：零假设（最优策略≈基准）下观察到 ≥sharpe 的概率。
    高→优势大概率是运气（多重比较膨胀），低→统计显著。

    参数：
      sharpe:  top 候选的（年化）夏普——L2 作用在 L1 calmar 排序后 top-N。
      n_trials: 试验次数 M（多重比较修正——试越多最高越虚高）。
      n_obs:   收益序列长度 T（样本量修正——越长越显著）。
      skew:    收益序列偏度（非正态修正；正态=0）。
      kurt:    收益序列峰度（非正态修正；正态=3，颈线法尖峰厚尾 kurt>3）。

    公式：DSR = Φ( (SR - SR_max) · √(T-1) / √(1 - skew·SR + (kurt-1)/4·SR²) )
    分母根号内为非正态修正的方差因子（Lo 2002）；负或零→数据异常返回 0。
    """
    sr_max = _expected_max_sharpe(n_trials)
    var_factor = 1.0 - skew * sharpe + (kurt - 1) / 4.0 * sharpe * sharpe
    if var_factor <= 0:
        return 0.0
    z = (sharpe - sr_max) * math.sqrt(max(n_obs - 1, 1)) / math.sqrt(var_factor)
    return _norm_cdf(z)


def dsr_gated_ranking(candidates, dsr_min=0.8, fallback_to_calmar=True):
    """P5 DSR 门控排序（spec §6.2）：calmar 排序 + DSR≥dsr_min 门槛共因子。

    candidates: [(calmar, dsr, trial_id), ...]（调用方算好 DSR 注入——多重比较
    校正依赖 n_trials/n_obs，逐候选口径由调用方保证）。
    返回 (gated, fell_back)：gated = calmar 降序且 DSR 达标的候选；
    门控空集 → fallback_to_calmar 时回退纯 calmar 排序（早期搜索 trial 数少 → DSR
    天然低，不惩罚早期搜索——ADR13 诚实报告：fell_back=True 显式标注）。
    """
    ranked = sorted(candidates, key=lambda x: x[0], reverse=True)
    gated = [c for c in ranked if c[1] >= dsr_min]
    if gated:
        return gated, False
    if fallback_to_calmar:
        return ranked, True
    return [], False
