# -*- coding: utf-8 -*-
"""参数空间覆盖度度量（spec §3.5 判据④，防伪收敛核心，纯函数）。

物理意图（spec §3.5 ⚠ 伪收敛陷阱）：收敛判据①②③ 只证"当前采样策略下没新东西"，
不能证"参数空间被充分探索"。若初始采样有盲区（21 维随机/贪心极易整片错过有效区域），
判据①会在采样不足的前沿上早早命中、自停，交付"撞到的孤峰"冒充相对最优。
**故判据④（覆盖度）是①②③ 的前置否决**——覆盖度不达标，判据①命中也不许停。

度量取网格单元占用率（design 决策2）：21 维每维按候选档分箱（候选档即箱），每个采样
params → 每维候选档索引 → 组合元组；ρ = 不重复组合数 / 总单元数（Π len(cand)）。
简单显式纯函数可单测，离散候选档天然分箱无需 KDE。ρ=0.8（spec §3.5 初定）。
"""


def grid_coverage(sampled_params, param_space=None):
    """网格单元占用率 ρ ∈ [0,1]（判据④度量，纯函数）。

    sampled_params: list[dict]（21 维，值在候选档内——sampler/sample_search/normalize 保证）。
    param_space: [(key, [candidates]), ...]，默认 sampler.PARAM_SPACE。
    ρ = 不重复候选档组合数 / Π len(candidates)。重复组合不算（去重）。
    """
    if param_space is None:
        from discovery.sampler import PARAM_SPACE
        param_space = PARAM_SPACE
    keys = [k for k, _ in param_space]
    # 每维 值→索引 映射（候选档值唯一可索引）
    cand_idx = [{c: i for i, c in enumerate(cands)} for _, cands in param_space]
    total = 1
    for _, cands in param_space:
        total *= len(cands)
    if total == 0:
        return 0.0
    seen = set()
    for p in sampled_params:
        # 值→索引元组（缺键/值不在候选档跳过——防御，正常路径不会触发）
        try:
            combo = tuple(cand_idx[d][p[keys[d]]] for d in range(len(keys)))
        except (KeyError, IndexError):
            continue
        seen.add(combo)
    return len(seen) / total


def coverage_gate(rho, threshold=0.8):
    """判据④：覆盖度是否达标（ρ≥threshold）。

    spec §3.5：覆盖度是判据①的前置否决——本函数返回 True 才允许判据①②自停；
    返回 False 时即便前沿不扩张、EI<ε 也不许停（须扩采样继续探索，防伪收敛）。
    threshold=0.8（spec §3.5 初定；实际标定留 Plan 4 daemon 跑后回溯，见 design §6）。
    """
    return rho >= threshold
