# -*- coding: utf-8 -*-
"""task.json / result.json 协议:dataclass + JSON 序列化。

物理定位(C3):纯数据载体,处理 date↔str、确保中文 ensure_ascii=False 保真。
compute_unit(Mac)与 task_export(Win)共用同一协议,保证跨机字节级一致。

v2(P0-2 · 2026-08-02):Task 增加 replay 模式字段(mode/start/end/position_model)。
    mode="discovery"(默认,兼容 v1)= 老 kelly/calmar 参数搜索评估;
    mode="replay" = 用 backtest.replay 引擎按 ReplayReport 口径评估(与 Win 主回测同源)。

设计:Task/Result 是 dataclass;SegmentSpec 的 date 字段在 JSON 里序列化为
"YYYY-MM-DD" str(isoformat),反序列化 date.fromisoformat 还原为 datetime.date。
metrics dict(inner/outer)原样透传(数值已是原生 float,evaluate 直出)。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path


# ── task.json 内嵌的 split 段(对应 discovery.split.Segment)──
@dataclass
class SegmentSpec:
    """一段日期区间(inner test / outer holdout)的序列化形态。"""
    name: str
    start: date
    end: date

    def to_dict(self) -> dict:
        return {"name": self.name, "start": self.start.isoformat(), "end": self.end.isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> "SegmentSpec":
        return cls(name=d["name"], start=date.fromisoformat(d["start"]),
                   end=date.fromisoformat(d["end"]))


@dataclass
class SplitSpec:
    """HoldoutSplit 的序列化形态(inner + outer + embargo_days)。"""
    inner: SegmentSpec
    outer: SegmentSpec
    embargo_days: int

    def to_dict(self) -> dict:
        return {"inner": self.inner.to_dict(), "outer": self.outer.to_dict(),
                "embargo_days": self.embargo_days}

    @classmethod
    def from_dict(cls, d: dict) -> "SplitSpec":
        return cls(inner=SegmentSpec.from_dict(d["inner"]),
                   outer=SegmentSpec.from_dict(d["outer"]),
                   embargo_days=d["embargo_days"])


@dataclass
class SnapshotMetaSpec:
    """discovery.snapshot.SnapshotMeta 的序列化形态(Win 权威,Mac 不重算)。"""
    snapshot_hash: str
    universe_def: str
    universe_count: int
    date_range: str
    lake_start: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SnapshotMetaSpec":
        return cls(**d)


@dataclass
class TrialSpec:
    """单条 trial:Win 端预算的 trial_id + params + source + seed(C8)。"""
    trial_id: str
    params: dict
    source: str
    seed: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TrialSpec":
        return cls(**d)


@dataclass
class Task:
    """task.json 的反序列化形态(Mac 端读取)。

    Win 端 task_export 写入,Mac 端 compute_unit 读取。三件哈希(git_commit/engine_hash/
    parquet_sha256)+ snapshot_meta 是 env_check 校验输入(C4)。
    """
    protocol_version: int
    task_id: str
    created_at: str
    git_commit: str
    engine_hash: str
    parquet_sha256: str
    lake_start: str
    embargo_days: int
    snapshot_meta: SnapshotMetaSpec
    split: SplitSpec
    trials: list
    # v2 (P0-2)：评估模式与 replay 模式参数。v1 老 task 无这些键 → 默认 discovery。
    mode: str = "discovery"          # "discovery"（kelly/calmar 搜索）| "replay"（replay 引擎）
    start: str | None = None         # replay 模式：显式单段区间起（None → 用 split.inner/outer）
    end: str | None = None           # replay 模式：显式单段区间止
    position_model: dict = field(default_factory=dict)   # PositionModel.to_dict()，空=默认

    def to_dict(self) -> dict:
        return {
            "protocol_version": self.protocol_version, "task_id": self.task_id,
            "created_at": self.created_at, "git_commit": self.git_commit,
            "engine_hash": self.engine_hash, "parquet_sha256": self.parquet_sha256,
            "lake_start": self.lake_start, "embargo_days": self.embargo_days,
            "snapshot_meta": self.snapshot_meta.to_dict(), "split": self.split.to_dict(),
            "trials": [t.to_dict() for t in self.trials],
            "mode": self.mode, "start": self.start, "end": self.end,
            "position_model": self.position_model,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            protocol_version=d["protocol_version"], task_id=d["task_id"],
            created_at=d["created_at"], git_commit=d["git_commit"],
            engine_hash=d["engine_hash"], parquet_sha256=d["parquet_sha256"],
            lake_start=d["lake_start"], embargo_days=d["embargo_days"],
            snapshot_meta=SnapshotMetaSpec.from_dict(d["snapshot_meta"]),
            split=SplitSpec.from_dict(d["split"]),
            trials=[TrialSpec.from_dict(t) for t in d["trials"]],
            mode=d.get("mode", "discovery"),
            start=d.get("start"),
            end=d.get("end"),
            position_model=d.get("position_model") or {},
        )

    @classmethod
    def from_json(cls, path) -> "Task":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_json(self, path) -> None:
        # ensure_ascii=False 保中文(universe_def 等);indent=2 pretty 便于人读/git diff
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


# ── result.json ──
@dataclass
class TrialResult:
    """单条 trial 评估结果。status: ok | failed | degenerate。

    report：replay 模式的内段报告快照（ReplayReport 压缩字段，无 trades/equity_curve），
    discovery 模式为空 dict。
    """
    trial_id: str
    status: str
    inner: dict = field(default_factory=dict)
    outer: dict = field(default_factory=dict)
    n_total: int = 0
    error: str = ""
    report: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TrialResult":
        return cls(**d)


@dataclass
class Result:
    """result.json 的序列化形态(Mac 本地,不回传)。"""
    task_id: str
    git_commit: str
    parquet_sha256: str
    ran_at: str
    results: list

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "git_commit": self.git_commit,
                "parquet_sha256": self.parquet_sha256, "ran_at": self.ran_at,
                "results": [r.to_dict() for r in self.results]}

    @classmethod
    def from_dict(cls, d: dict) -> "Result":
        return cls(task_id=d["task_id"], git_commit=d["git_commit"],
                   parquet_sha256=d["parquet_sha256"], ran_at=d["ran_at"],
                   results=[TrialResult.from_dict(r) for r in d["results"]])

    def to_json(self, path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def from_json(cls, path) -> "Result":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
