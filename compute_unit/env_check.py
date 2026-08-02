# -*- coding: utf-8 -*-
"""跨机一致性校验:三件哈希 + snapshot 双校验(C4)。

哈希原语在 compute_unit.hashes(env_check + task_export 共享,DRY)。

基石物理意图:discovery 的可比性靠 snapshot_hash + engine_hash + trial_id 三件套。Mac
计算单元必须保证这三件与 Win 字节级一致,否则 Mac 跑的是脏数据,摘要里的 calmar 和 Win
补跑的对不上,整套筛选失去意义。任一漂移 → EnvDriftError,compute_unit 拒跑(退出码 3)。

两层校验:
- _check_hashes:git_commit / engine_hash / parquet_sha256 三件(纯文件 + git,不依赖 freeze)
- _check_snapshot:Mac 本地 freeze 重算 universe_count / date_range vs task.snapshot_meta
  (捕获哈希漏掉的逻辑层漂移:is_target_board / load_universe 流动性阈值改了)
"""
from __future__ import annotations

from compute_unit.hashes import _engine_hash, _file_sha256, _git_head_sha, parquet_path as _parquet_path
from compute_unit.protocol import Task


class EnvDriftError(RuntimeError):
    """环境漂移(代码/数据/快照不一致),compute_unit 拒跑(退出码 3)。"""


def _check_hashes(task: Task) -> None:
    """三件哈希校验:git_commit / engine_hash / parquet_sha256。任一不符抛 EnvDriftError。"""
    # ① git commit(代码版本)
    head = _git_head_sha()
    if head != task.git_commit:
        raise EnvDriftError(
            f"git_commit 漂移:Mac 本地 {head[:12]} ≠ task {task.git_commit[:12]},请 git pull")
    # ② engine hash(回测内核代码指纹)
    eng = _engine_hash()
    if eng != task.engine_hash:
        raise EnvDriftError(
            f"engine_hash 漂移:Mac 本地 {eng} ≠ task {task.engine_hash}"
            "(回测内核文件不一致,请 git pull)")
    # ③ parquet sha256(数据指纹)
    pq = _file_sha256(_parquet_path())
    if pq != task.parquet_sha256:
        raise EnvDriftError(
            f"parquet_sha256 漂移:Mac 本地 {pq[:12]}… ≠ task {task.parquet_sha256[:12]}…"
            "(数据滞后或损坏,请 git pull 最新 parquet)")


def _check_snapshot(task: Task, meta) -> None:
    """snapshot 双校验:Mac freeze 出的 universe_count/date_range vs task.snapshot_meta。

    meta: discovery.snapshot.SnapshotMeta(freeze 返回)。捕获哈希漏掉的逻辑层漂移。
    """
    if meta.universe_count != task.snapshot_meta.universe_count:
        raise EnvDriftError(
            f"universe_count 漂移:Mac freeze {meta.universe_count} "
            f"≠ task {task.snapshot_meta.universe_count}")
    if meta.date_range != task.snapshot_meta.date_range:
        raise EnvDriftError(
            f"date_range 漂移:Mac {meta.date_range} ≠ task {task.snapshot_meta.date_range}")


def verify_and_freeze(task: Task):
    """校验 + freeze,返回 (universe, meta)。runner.run 复用,避免重复 freeze(~5s/次)。

    流程:_check_hashes(纯文件/git)→ freeze(lake_start)重算 meta → _check_snapshot(meta)。
    漂移任一环节抛 EnvDriftError。通过则返回 universe 供后续跑批复用(不重读 parquet)。
    """
    _check_hashes(task)
    from discovery.snapshot import freeze
    universe, meta = freeze(lake_start=task.lake_start)
    _check_snapshot(task, meta)
    return universe, meta


def verify(task: Task) -> None:
    """完整校验(CLI verify 子命令用,只校验不跑批)。"""
    verify_and_freeze(task)   # 丢弃返回的 universe
