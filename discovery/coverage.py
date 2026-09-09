# -*- coding: utf-8 -*-
"""参数空间覆盖度度量（spec §3.5 判据④，防伪收敛核心，纯函数）。

物理意图（spec §3.5 ⚠ 伪收敛陷阱）：收敛判据①②③ 只证"当前采样策略下没新东西"，
不能证"参数空间被充分探索"。若初始采样有盲区（21 维随机/贪心极易整片错过有效区域），
判据①会在采样不足的前沿上早早命中、自停，交付"撞到的孤峰"冒充相对最优。
**故判据④（覆盖度）是①②③ 的前置否决**——覆盖度不达标，判据①命中也不许停。

2026-09-09 度量修订（原联合占用率数学不可达）：原度量 = 不重复候选档**组合**数 /
Π len(cand)（21 维笛卡尔积）。总单元数随维数乘法爆炸——ρ≥0.8 需要 ≥0.8×总单元
数的不重复样本，数百上千 trial 的搜索预算下不可达 → 判据④恒否决 = 单夜自停
失效（只能跑满预算外停，"实际标定留 daemon 跑后回溯"的欠账今日清账）。修订后
度量 = **逐维边际覆盖的最小值**（worst-dim marginal coverage）：每维「候选档被
采到的比例」取全维 min——语义从「网格无组合盲区」（不可达）收缩为「每一维的
候选档都无盲区」（数百样本可达），防伪收敛意图不变（任一维仍有整片未采档位 →
不许自停）。
"""


def grid_coverage(sampled_params, param_space=None):
    """逐维边际覆盖 ρ ∈ [0,1]（判据④度量，纯函数，2026-09-09 修订版）。

    sampled_params: list[dict]（21 维，值在候选档内——sampler/sample_search/normalize 保证）。
    param_space: [(key, [candidates]), ...]，默认 sampler.PARAM_SPACE。
    ρ = min_d (该维被采到的不同候选档数 / len(cand_d))；空采样返 0.0。
    值不在候选档内不计（防御，正常路径不会触发）。
    """
    if param_space is None:
        from discovery.sampler import PARAM_SPACE
        param_space = PARAM_SPACE
    if not sampled_params:
        return 0.0
    worst = 1.0
    for key, cands in param_space:
        if not cands:
            continue
        seen = set()
        for p in sampled_params:
            # 缺键/值不在候选档跳过（防御，与原联合度量同款；注意候选档可含
            # None（参数关闭档），不能用 p.get(key) 判在册——缺键 get 返 None
            # 会误命中 None 档再 KeyError）。
            try:
                v = p[key]
            except (KeyError, IndexError, TypeError):
                continue
            if v in cands:
                seen.add(v)
        worst = min(worst, len(seen) / len(cands))
    return worst


def coverage_gate(rho, threshold=0.8):
    """判据④：覆盖度是否达标（ρ≥threshold）。

    spec §3.5：覆盖度是判据①的前置否决——本函数返回 True 才允许判据①②自停；
    返回 False 时即便前沿不扩张、EI<ε 也不许停（须扩采样继续探索，防伪收敛）。
    threshold=0.8（spec §3.5 初定；度量改边际后含义 = 每一维 ≥80% 候选档被采过）。
    """
    return rho >= threshold
