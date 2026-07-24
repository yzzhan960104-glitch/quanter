# -*- coding: utf-8 -*-
"""Pareto 前沿 + 收敛判据① 测试（纯函数，零依赖）。"""


def test_pareto_frontier_2d():
    """2 目标 (ann↑, max_dd↓) 非支配前沿：被支配的剔除。"""
    from discovery.pareto import pareto_frontier
    trials = [
        {"ann": 0.5, "max_dd": 0.20},   # 0：被 1 支配（1 ann 更高 + max_dd 更低）
        {"ann": 0.6, "max_dd": 0.15},   # 1：前沿
        {"ann": 0.4, "max_dd": 0.10},   # 2：前沿（max_dd 最低）
        {"ann": 0.3, "max_dd": 0.30},   # 3：被 0/1/2 支配
    ]
    idx = pareto_frontier(trials)   # 默认 obj_max=("ann",), obj_min=("max_dd",)
    assert set(idx) == {1, 2}


def test_pareto_frontier_single_obj():
    """单目标 ann↑：前沿=最高 ann 那 trial。"""
    from discovery.pareto import pareto_frontier
    trials = [{"ann": 0.1}, {"ann": 0.5}, {"ann": 0.3}]
    idx = pareto_frontier(trials, obj_max=("ann",), obj_min=())
    assert idx == [1]


def test_frontier_grew():
    """新前沿有 old 没有的点=扩张。"""
    from discovery.pareto import frontier_grew
    assert frontier_grew([1, 2], [1, 2, 3]) is True
    assert frontier_grew([1, 2], [1, 2]) is False
    assert frontier_grew([1, 2, 3], [1, 2]) is False   # 收缩不算扩张


def test_converged_k_rounds_true():
    """连续 K 轮前沿不扩张→收敛（判据①）。"""
    from discovery.pareto import converged_k_rounds
    # 第 0 轮 {1}，第 1 轮扩张到 {1,2}，第 2/3/4 轮都不扩张
    history = [[1], [1, 2], [1, 2], [1, 2], [1, 2]]
    assert converged_k_rounds(history, K=3) is True


def test_converged_k_rounds_false_recent_grew():
    """最近一轮扩张→未收敛。"""
    from discovery.pareto import converged_k_rounds
    history = [[1], [1, 2], [1, 2], [1, 2], [1, 2, 3]]   # 最后一轮扩张
    assert converged_k_rounds(history, K=3) is False


def test_converged_k_rounds_false_short_history():
    """历史不足 K+1 轮→无法判收敛（False，保守不停）。"""
    from discovery.pareto import converged_k_rounds
    assert converged_k_rounds([[1], [1, 2]], K=3) is False
