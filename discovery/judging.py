# -*- coding: utf-8 -*-
"""分层裁判最小版（spec §3.5 v1.2，Plan 1 只做 L0 闸 + L1 calmar）。

L0 可行域闸：max_dd≤0.4 ∧ n≥30。熊市 ann≥0 在 Plan 1 标 N/A——2025-2026 无熊市数据，
熊市一票否决待后续 plan 扩 regime 数据（spec §3.3 硬约束 + §1.4 真风险 regime 依赖）。
L1 主目标：可行域内按 calmar=ann/max_dd 降序全序（颈线法主风险是回撤，calmar 比 sharpe 贴）。
L2 DSR / L3 邻域留后续 plan（Plan 1 邻域稳定性在 Task 7 手动验收，不进排序）。
"""
FEASIBILITY_MAX_DD = 0.4    # spec §3.5 L0：回撤上限
FEASIBILITY_MIN_N = 30      # spec §3.5 L0：最小交易笔数（统计意义）


def feasibility_gate(metrics, max_dd_max=FEASIBILITY_MAX_DD, n_min=FEASIBILITY_MIN_N):
    """L0 可行域闸：max_dd≤阈值 ∧ n≥阈值（熊市项 Plan 1 N/A）。"""
    return metrics["max_dd"] <= max_dd_max and metrics["n"] >= n_min


def calmar_rank(candidates):
    """L1 主目标排序：可行域内按 calmar 降序全序。candidates = list[metrics dict]。"""
    feasible = [c for c in candidates if feasibility_gate(c)]
    return sorted(feasible, key=lambda c: c.get("calmar", 0.0), reverse=True)
