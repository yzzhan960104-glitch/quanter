# -*- coding: utf-8 -*-
"""邻域稳定性（spec §12⑥ / 分层裁判 L3，Plan 1 手动验收版）。

冠军 21 维数值参数 ±perturb 扰动采样，跑 evaluate 看 outer calmar 稳定性。
高原（邻域 calmar 不塌、方差有限）= 稳健放行；孤峰（塌或方差爆炸）= 过拟合否决。
Plan 1 手动验收（verify 命令），不进 calmar_rank 排序（排序留 Plan 3 搜索）。
百分比扰动（×[1-p,1+p]），不依赖 PARAM_SPACE 候选档 → discovery 自洽。

物理意图拷问三连（CLAUDE.md 风控人格）：
- 流动性/极端行情：邻域扰动是参数维扰动，不触行情；但 evaluate 内 scan_symbol 走真实
  流动性边界（amount≥1e5），扰动 window 可能滑过/滑入边界票——这是真实鲁棒性检验，非缺陷。
- 接口边界：evaluate 内已 try-except 吞单标的异常（run_full_scan），扰动后个别标的崩
  不会让整次邻域采样失败，稳。
- 策略风险敞口：邻域只看 outer calmar 是否塌——若冠军是过拟合尖峰（仅一组极窄参数出高 calmar），
  邻域采样必塌，is_plateau=False 触发否决，防止实盘押注一个不可重现的尖峰。
"""
import random

import pandas as pd

from discovery.objective import evaluate


def perturb_params(params, perturb, rng, n_dims=3):
    """随机选 n_dims 个数值参数 ×[1-perturb, 1+perturb]。None 参数保留。

    int 参数（window/min_touches 等）扰动后 round 保 int——否则 scan_symbol 的
    range(window) 收到 float 会 TypeError（颈线法内核 hardcode 依赖 int window，
    range(80.0) 直接报错）。

    物理意图：百分比扰动（而非跳到 PARAM_SPACE 邻档）让 discovery 自洽——不依赖
    scripts/param_iter 的候选值定义，本模块可独立判稳定性。None 保留是语义安全要求
    （None 在颈线法=用内核默认/不启用该机制，扰动 None 会破坏语义）。
    """
    nb = dict(params)
    # 只扰数值键（int/float）；None/其他类型原样保留
    numeric_keys = [k for k, v in params.items() if isinstance(v, (int, float))]
    # bool 是 int 子类，但颈线法 params 无 bool 键；保守起见不把 bool 当数值扰
    numeric_keys = [k for k in numeric_keys if not isinstance(params[k], bool)]
    keys = rng.sample(numeric_keys, k=min(n_dims, len(numeric_keys)))
    for k in keys:
        new_v = params[k] * rng.uniform(1 - perturb, 1 + perturb)
        # int 键 round 保 int（range(window) 防 TypeError）；float 键原样
        nb[k] = round(new_v) if isinstance(params[k], int) else new_v
    return nb


def neighborhood_stability(params, universe, split, perturb=0.15, n_samples=5, seed=42):
    """冠军邻域 ±perturb 扰动 n_samples 次，看 outer calmar 稳定性。

    返回 {base_calmar, neighbor_calmars, neighbor_mean, std, is_plateau, base_outer}。
    is_plateau 判据：邻域 calmar 均值 ≥ base×0.5（不塌过半）。孤峰（均值远低于 base）
    → is_plateau=False → spec §12⑥ 否决。

    物理意图拷问：
    - base_c≤0（冠军 outer 本就亏）：判 mean≥0——邻域不亏才算"勉强高原"，否则孤峰否决。
    - n_samples=1 时 std 退化成 0.0（pd.Series.std 单点无意义），不影响 is_plateau 判定。
    - 默认 perturb=0.15（±15%）是经验值：太小（如 5%）邻域太窄≈原地重复检不出尖峰；
      太大（如 50%）邻域跑出有意义参数区间。±15% 是"小幅扰动检验局部稳健性"的常规取值。
    - seed 固定 42 保证可复现（与 spec §3.2 双指纹的复现精神一致）。
    """
    rng = random.Random(seed)
    base_outer = evaluate(params, universe, split)["outer"]
    base_c = base_outer["calmar"]
    neighbor_calmars = []
    for _ in range(n_samples):
        # 默认 n_dims=3：每采样扰 3 维（21 维全扰会偏离太远，3 维小幅局部扰动够检验稳健性）
        nb = perturb_params(params, perturb, rng)
        neighbor_calmars.append(evaluate(nb, universe, split)["outer"]["calmar"])
    s = pd.Series(neighbor_calmars)
    mean = float(s.mean())
    # 单点 std 无意义（NaN），退化为 0.0；多点走 pd.Series.std（默认 ddof=1 样本方差）
    std = float(s.std()) if len(s) > 1 else 0.0
    # 高原判据：邻域均值不塌过半（base>0 时）；base≤0 时邻域不亏（mean≥0）算勉强高原
    is_plateau = (mean >= base_c * 0.5) if base_c > 0 else (mean >= 0)
    return {"base_calmar": base_c, "neighbor_calmars": neighbor_calmars,
            "neighbor_mean": mean, "std": std, "is_plateau": bool(is_plateau),
            "base_outer": base_outer}
