# -*- coding: utf-8 -*-
"""L4 Pareto 非支配前沿 + 收敛判据①③（spec §3.5 v1.2 / §4.1，纯函数）。

物理意图（spec §3.5 v1.2）：v1 把 Pareto 当主排序，前沿在 21 维高维稀疏 + 噪声敏感下
"一堆点谁也压不住谁"。v1.2 降级——主排序用单目标 calmar（L1 全序），Pareto 退为
"L1 候选筛选补充"：前沿上的解进 L1 全序。本模块只算前沿（多目标非支配），不排序
（排序在 judging.calmar_rank）。默认 2 目标 ann↑/max_dd↓（颈线法最关切的收益-回撤前沿；
sharpe 与 ann 高度相关，n 笔数在 L0 闸 n≥30 已约束，故不进 Pareto 维度）。

收敛判据①（spec §3.5）：连续 K 轮新 trial 无一进 Pareto 前沿（前沿不扩张）→ 收敛自停。
本模块提供 frontier_grew + converged_k_rounds；判据④覆盖度前置否决在 coverage.py，
判据②EI 在 search.py，判据③预算耗尽即 Plan 2 的 budget_exhausted。
"""


def pareto_frontier(trials, obj_max=("ann",), obj_min=("max_dd",)):
    """Pareto 非支配前沿（纯函数）。返回非支配 trial 的索引列表。

    obj_max: 越大越好的目标键（如 ann）；obj_min: 越小越好的目标键（如 max_dd）。
    非支配定义：trial i 不被任何 j 支配；j 支配 i ⟺ j 在所有目标上 ≥/≤ i（方向匹配）
    且至少一个目标严格优于。O(n²)——颈线法单 run trial 数 ≤ 千级，可接受。
    """
    n = len(trials)
    frontier = []
    for i in range(n):
        dominated = False
        for j in range(n):
            if i == j:
                continue
            ti, tj = trials[i], trials[j]
            # j 在 max 目标全 ≥ i，且 min 目标全 ≤ i
            ge_all = all(tj[k] >= ti[k] for k in obj_max)
            le_all = all(tj[k] <= ti[k] for k in obj_min)
            if not (ge_all and le_all):
                continue
            # 至少一个目标严格优于（否则相等不算支配）
            strict = (any(tj[k] > ti[k] for k in obj_max) or
                      any(tj[k] < ti[k] for k in obj_min))
            if strict:
                dominated = True
                break
        if not dominated:
            frontier.append(i)
    return frontier


def frontier_grew(old_frontier, new_frontier):
    """新前沿是否有 old 没有的点（前沿扩张）。

    new ⊆ old → 未扩张（False）；new 有 old 没有的点 → 扩张（True）。
    收缩（new 比 old 小）不算扩张——前沿只会因新非支配点而扩张。
    """
    return not set(new_frontier).issubset(set(old_frontier))


def converged_k_rounds(frontier_history, K=3):
    """连续 K 轮前沿不扩张 → 收敛（判据①，spec §3.5）。

    frontier_history: list[list[int]]，每轮的 Pareto 前沿索引集。
    需至少 K+1 轮历史（最近 K 轮各自对比前一轮）；不足则保守返回 False（不停）。
    """
    if len(frontier_history) < K + 1:
        return False
    # 最近 K 轮（索引 len-K .. len-1）各自相对前一轮都不扩张
    for r in range(len(frontier_history) - K, len(frontier_history)):
        if frontier_grew(frontier_history[r - 1], frontier_history[r]):
            return False
    return True
