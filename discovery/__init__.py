# -*- coding: utf-8 -*-
"""参数发现引擎（spec 2026-07-23-param-discovery-engine-design.md v1.3）。

Plan 1（L0+L1 可信度闭环）：快照冻结 + 2025/2026 holdout 嵌套 OOS + 分层裁判最小版。
"""
from discovery.snapshot import freeze, SnapshotMeta, snapshot_hash
from discovery.split import holdout_split, Segment, HoldoutSplit
from discovery.objective import evaluate, run_full_scan, segment_metrics, metrics_of
from discovery.judging import feasibility_gate, calmar_rank

__all__ = ["freeze", "SnapshotMeta", "snapshot_hash", "holdout_split", "Segment",
           "HoldoutSplit", "evaluate", "run_full_scan", "segment_metrics", "metrics_of",
           "feasibility_gate", "calmar_rank"]
