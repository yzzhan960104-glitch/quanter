# -*- coding: utf-8 -*-
"""参数发现引擎（spec 2026-07-23-param-discovery-engine-design.md v1.3）。

Plan 1（L0+L1 可信度闭环）：快照冻结 + 2025/2026 holdout 嵌套 OOS + 分层裁判最小版。
Plan 2（L2 吞吐 + L3 搜索基础）：约束裁剪 + Sobol/random 采样 + ProcessPool 并发 + 断点续跑。
"""
from discovery.snapshot import freeze, SnapshotMeta, snapshot_hash
from discovery.split import holdout_split, Segment, HoldoutSplit
from discovery.objective import evaluate, run_full_scan, segment_metrics, metrics_of
from discovery.judging import feasibility_gate, calmar_rank
# Plan 2 新增
from discovery.constraints import normalize_params, is_feasible, filter_feasible, PARAM_KEYS
from discovery.sampler import sample_search, sobol_sample, random_sample, PARAM_SPACE
from discovery.worker import eval_batch
from discovery.runner import run_search, RunSummary
# Plan 3 新增
from discovery.pareto import pareto_frontier, frontier_grew, converged_k_rounds
from discovery.coverage import grid_coverage, coverage_gate
from discovery.dsr import deflated_sharpe
from discovery.search import tpe_search, expected_improvement
# Plan 4 新增：L4 daemon 生产入口（cli cmd_daemon 调）
from discovery.daemon import run_daemon, run_daemon_cycle

__all__ = ["freeze", "SnapshotMeta", "snapshot_hash", "holdout_split", "Segment",
           "HoldoutSplit", "evaluate", "run_full_scan", "segment_metrics", "metrics_of",
           "feasibility_gate", "calmar_rank",
           # Plan 2
           "normalize_params", "is_feasible", "filter_feasible", "PARAM_KEYS",
           "sample_search", "sobol_sample", "random_sample", "PARAM_SPACE",
           "eval_batch", "run_search", "RunSummary",
           # Plan 3
           "pareto_frontier", "frontier_grew", "converged_k_rounds",
           "grid_coverage", "coverage_gate", "deflated_sharpe",
           "tpe_search", "expected_improvement",
           # Plan 4
           "run_daemon", "run_daemon_cycle"]
