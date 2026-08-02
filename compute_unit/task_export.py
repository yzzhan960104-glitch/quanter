# -*- coding: utf-8 -*-
"""Win 端 task.json 导出:params 批 + 当前 freeze meta + 三件哈希 → tasks/<id>.json。

物理定位:Win 主机专用(Mac 不用)。把 discovery Sobol 采样的一批 params 打包成 Mac 可拉的
task.json。trial_id 在此预算(discovery.store.trial_id_of,C8),Mac 不算——跨机一致性的
权威来源。

用法:
  python -m compute_unit.task_export --params-file sobol_batch.json --out tasks/<id>.json
  (sobol_batch.json 内容为 params dict 的 list,如 [{"window":80,...}, {...}] )
"""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime
from pathlib import Path

# 三件哈希原语复用 hashes.py(DRY 单源,与 env_check 同款算法)——pre-flight 裁决:
# brief 原本在 task_export 本地重声明 _git_head_sha/_file_sha256/_engine_hash,Task 2 已抽出
# 共享模块,故此处 from import,避免算法漂移(改算法只改 hashes.py 一处)。
from compute_unit.hashes import _engine_hash, _file_sha256, _git_head_sha, parquet_path
from compute_unit.protocol import (
    Task, TrialSpec, SnapshotMetaSpec, SplitSpec, SegmentSpec,
)


def _holdout_to_spec() -> tuple:
    """discovery.split.holdout_split() → (SplitSpec, embargo_days) 序列化形态。"""
    from discovery.split import holdout_split
    sp = holdout_split()   # 默认 embargo_days=5,inner 2025 / outer 2026
    spec = SplitSpec(
        inner=SegmentSpec(sp.inner.name, sp.inner.start, sp.inner.end),
        outer=SegmentSpec(sp.outer.name, sp.outer.start, sp.outer.end),
        embargo_days=sp.embargo_days,
    )
    return spec, sp.embargo_days


def _snapshot_to_spec(meta) -> SnapshotMetaSpec:
    """discovery.snapshot.SnapshotMeta → SnapshotMetaSpec 序列化形态。"""
    return SnapshotMetaSpec(
        snapshot_hash=meta.snapshot_hash, universe_def=meta.universe_def,
        universe_count=meta.universe_count, date_range=meta.date_range,
        lake_start=meta.lake_start,
    )


def export_task(params_list: list, lake_start: str = "2025-01-01",
                seed: int = 42, source: str = "discovery_search",
                out_path="tasks/task.json", mode: str = "discovery",
                start: str | None = None, end: str | None = None,
                position_model: dict | None = None) -> Task:
    """params 批 → Task → 写 out_path。

    流程:freeze 取 SnapshotMeta → 算 trial_id(trial_id_of)→ 算三件哈希(git/engine/parquet)
    → 拼 Task → to_json。trial_id 在此预算(Win 权威 C8),Mac 不算。

    v2(P0-2):mode="replay" 时带 start/end/position_model,Mac 端跑 backtest.replay
    引擎(ReplayReport 口径);mode 默认 "discovery"(老 kelly/calmar 搜索,兼容 v1)。
    """
    from discovery.snapshot import freeze
    from discovery.store import trial_id_of

    _universe, meta = freeze(lake_start=lake_start)
    split_spec, embargo_days = _holdout_to_spec()

    trials = [
        TrialSpec(trial_id=trial_id_of(p, meta.snapshot_hash, seed),
                  params=p, source=source, seed=seed)
        for p in params_list
    ]
    task = Task(
        protocol_version=2 if mode == "replay" else 1,
        task_id=uuid.uuid4().hex[:8],
        created_at=datetime.utcnow().isoformat(),
        git_commit=_git_head_sha(),
        engine_hash=_engine_hash(),
        parquet_sha256=_file_sha256(parquet_path()),
        lake_start=lake_start,
        embargo_days=embargo_days,
        snapshot_meta=_snapshot_to_spec(meta),
        split=split_spec,
        trials=trials,
        mode=mode,
        start=start,
        end=end,
        position_model=position_model or {},
    )
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    task.to_json(out_path)
    return task


def main(argv: list[str] | None = None) -> int:
    """CLI:python -m compute_unit.task_export --params-file <json> --out <path>。"""
    p = argparse.ArgumentParser(prog="python -m compute_unit.task_export",
                                description="Win 端导出 task.json(Mac 拉取跑批)")
    p.add_argument("--params-file", required=True, help="JSON 文件,内容为 params dict 的 list")
    p.add_argument("--out", default="tasks/task.json", help="task.json 输出路径")
    p.add_argument("--lake-start", default="2025-01-01", help="freeze 加载起始日")
    p.add_argument("--seed", type=int, default=42, help="trial_id seed")
    p.add_argument("--mode", choices=["discovery", "replay"], default="discovery",
                   help="评估模式：discovery=kelly/calmar 搜索（默认）；replay=backtest.replay 引擎")
    p.add_argument("--start", default=None, help="replay 模式区间起（YYYY-MM-DD）")
    p.add_argument("--end", default=None, help="replay 模式区间止（YYYY-MM-DD）")
    p.add_argument("--position-model", default=None,
                   help="replay 模式资金模型 JSON（如 {\"pos_cap\": 0.05}）")
    args = p.parse_args(argv)

    params_list = json.loads(Path(args.params_file).read_text(encoding="utf-8"))
    position_model = json.loads(args.position_model) if args.position_model else None
    task = export_task(params_list, lake_start=args.lake_start, seed=args.seed,
                       out_path=args.out, mode=args.mode, start=args.start,
                       end=args.end, position_model=position_model)
    print(f"✅ 导出 task={task.task_id} → {args.out}({len(task.trials)}组)")
    print(f"   git={task.git_commit[:8]} engine={task.engine_hash} "
          f"parquet={task.parquet_sha256[:8]}…")
    print(f"   snapshot={task.snapshot_meta.snapshot_hash[:8]} "
          f"universe={task.snapshot_meta.universe_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
