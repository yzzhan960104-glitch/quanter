# -*- coding: utf-8 -*-
"""采样层测试：Sobol 均匀性 + random + 约束裁剪后全合法 + 可复现（快测，零 data_lake）。"""
import numpy as np
import pytest


def test_sobol_shape_and_range():
    """Sobol 输出 shape=(n, dim)，值∈[0,1)。"""
    from discovery.sampler import sobol_sample
    s = sobol_sample(dim=5, n=16, seed=42)
    assert s.shape == (16, 5)
    assert (s >= 0).all() and (s < 1).all()


def test_sobol_deterministic():
    """同 seed 同 dim 同 n → 同输出（可复现，落 trial.seed 的基石）。"""
    from discovery.sampler import sobol_sample
    a = sobol_sample(dim=4, n=8, seed=7)
    b = sobol_sample(dim=4, n=8, seed=7)
    assert np.array_equal(a, b)


def test_sobol_seed_advances():
    """不同 seed → 不同序列（seed 真起作用，非固定起点）。"""
    from discovery.sampler import sobol_sample
    a = sobol_sample(dim=4, n=8, seed=1)
    b = sobol_sample(dim=4, n=8, seed=2)
    assert not np.array_equal(a, b)


def test_sobol_uniformity_better_than_random():
    """Sobol 一维投影离散度 ≤ 纯随机（低差异序列，spec §3.5 判据④覆盖度物理手段）。

    判据：把 [0,1) 分 8 桶，Sobol 一维投影的桶计数方差应 ≤ 纯随机（均匀覆盖）。
    这是 spec §3.5 "判据④ Sobol 覆盖均匀性 ≥ 纯随机" 的最小可测化。
    """
    from discovery.sampler import sobol_sample, random_sample
    n = 64
    sob = sobol_sample(dim=3, n=n, seed=42)[:, 0]    # 第一维投影
    rnd = random_sample(dim=3, n=n, seed=42)[:, 0]
    bins = np.linspace(0, 1, 9)   # 8 桶
    sob_counts = np.histogram(sob, bins)[0]
    rnd_counts = np.histogram(rnd, bins)[0]
    assert sob_counts.std() <= rnd_counts.std() + 1e-9   # Sobol 桶计数方差 ≤ 随机


def test_random_sample_shape():
    from discovery.sampler import random_sample
    r = random_sample(dim=6, n=10, seed=1)
    assert r.shape == (10, 6)
    assert (r >= 0).all() and (r < 1).all()


def test_scale_to_candidates_picks_valid_levels():
    """单位向量 → 候选档索引 → 取 PARAM_SPACE 对应档（值在候选列表内）。"""
    from discovery.sampler import _scale_to_candidates, PARAM_SPACE
    # 单位向量接近 0 → 第一档；接近 1 → 最后一档
    unit_vec = np.array([0.0, 0.99])
    candidates_per_dim = [
        [40, 60, 80],            # window 3 档
        [None, 30, 60],          # decay_tau 3 档（含 None）
    ]
    p = _scale_to_candidates(unit_vec, candidates_per_dim)
    assert p[0] == 40             # 0.0 → 第一档
    assert p[1] == 60             # 0.99 → 最后一档


def test_sample_search_all_feasible():
    """sample_search 产出全部合法（经 filter_feasible，tp1≤tp_h + cancel≥tp1）。"""
    from discovery.sampler import sample_search
    from discovery.constraints import is_feasible
    batch = sample_search(n_sobol=20, n_random=10, seed=42)
    assert len(batch) >= 10       # 至少裁剪后有若干合法
    for p in batch:
        assert is_feasible(p) is True
        assert p["min_rr"] == 2.0              # normalize 过
        # trailing 一致性：grace=0 时 step/floor 必为 0
        if p["trailing_grace"] == 0:
            assert p["trailing_step"] == 0.0
            assert p["trailing_floor"] == 0.0


def test_sample_search_has_21_dims():
    """每条采样覆盖 21 维键。"""
    from discovery.sampler import sample_search
    from discovery.constraints import PARAM_KEYS
    batch = sample_search(n_sobol=5, n_random=5, seed=1)
    for p in batch:
        for k in PARAM_KEYS:
            assert k in p, f"缺参数 {k}"


def test_sample_search_reproducible():
    """同 seed 同 n → 同 batch（可复现）。"""
    from discovery.sampler import sample_search
    a = sample_search(n_sobol=10, n_random=5, seed=99)
    b = sample_search(n_sobol=10, n_random=5, seed=99)
    assert len(a) == len(b)
    # 每条 dict 值相等（None 也要相等）
    for x, y in zip(a, b):
        assert x == y


def test_sample_search_no_duplicates():
    """P2 fix（2026-08-13）：合法空间小（约束裁剪）时补采不重复——预算全落唯一组合。

    P1-1 基线实测 seed=42 的 72 组含 13 组重复（18% 评估浪费）——本测试钉死去重
    （同 seed 同规模采样必须全唯一；确定性不变）。
    """
    from discovery.sampler import sample_search
    sampled = sample_search(n_sobol=48, n_random=24, seed=42)
    keys = [tuple(sorted(p.items())) for p in sampled]
    assert len(sampled) == 72
    assert len(set(keys)) == 72, f"采样含 {len(sampled) - len(set(keys))} 组重复——去重失效"
