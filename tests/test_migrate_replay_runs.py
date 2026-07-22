# -*- coding: utf-8 -*-
"""migrate_replay_runs_to_sqlite 单测：JSON→SQLite 迁移正确性 + 幂等 + 损坏跳过（Task 8）。

迁移脚本在 scripts/（非 Python 包，无 __init__.py），故用 importlib 从文件路径加载，
避免 `from scripts import` 的包结构依赖。源 JSON 结构对齐 caisen.replay_runs.save_run。
"""
import importlib.util
import json
from pathlib import Path

from backtest import tasks_db as replay_tasks_db

# importlib 加载迁移脚本（scripts/ 非包；exec 时其顶部 sys.path.insert 项目根让 from caisen 可达）
_SPEC = importlib.util.spec_from_file_location(
    "migrate_replay_runs_to_sqlite",
    Path(__file__).resolve().parent.parent / "scripts" / "migrate_replay_runs_to_sqlite.py",
)
mig = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mig)


def _write_run(runs_dir, run_id, **over):
    """写一个老格式 replay_runs/<run_id>.json（对齐 save_run 落盘结构）。"""
    payload = {
        "run_id": run_id,
        "created_at": "2026-07-01T12:00:00.000000",
        "request": {"start": "2024-01-01", "end": "2024-06-01",
                    "universe": None, "cfg_override": {}, **over.pop("req", {})},
        "report": {"n_hits": 5, "win_rate": 0.6, **over.pop("rep", {})},
    }
    payload.update(over)
    (runs_dir / f"{run_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_migrate_json_to_sqlite(tmp_path):
    """单条 JSON → SQLite SUCCESS 行（report 内嵌 + universe/cfg_override 无损）。"""
    runs_dir = tmp_path / "replay_runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "20260701-120000-abcdef",
               req={"universe": ["000001.SZ"], "cfg_override": {"min_rr_ratio": 1.5}})
    (runs_dir / "index.json").write_text("[]", encoding="utf-8")   # 摘要列表，应被跳过

    db = str(tmp_path / "t.db")
    assert mig.migrate(str(runs_dir), db) == 1

    rows = replay_tasks_db.list_success_runs(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["task_id"] == "20260701-120000-abcdef"
    assert r["status"] == "SUCCESS"
    assert r["progress"] == 100
    assert r["universe"] == ["000001.SZ"]
    assert r["universe_n"] == 1
    assert r["cfg_override"] == {"min_rr_ratio": 1.5}
    assert r["report"]["n_hits"] == 5


def test_migrate_universe_none_is_all_market(tmp_path):
    """universe=None（全市场）→ universe_n=-1、universe_json=null。"""
    runs_dir = tmp_path / "replay_runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "20260705-120000-aaaaaa")   # request.universe 默认 None
    db = str(tmp_path / "t.db")
    mig.migrate(str(runs_dir), db)
    r = replay_tasks_db.list_success_runs(db)[0]
    assert r["universe"] is None
    assert r["universe_n"] == -1


def test_migrate_idempotent(tmp_path):
    """重复 migrate 不重复插入（已存在 task_id 跳过）。"""
    runs_dir = tmp_path / "replay_runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "20260701-120000-abcdef")
    db = str(tmp_path / "t.db")
    assert mig.migrate(str(runs_dir), db) == 1
    assert mig.migrate(str(runs_dir), db) == 0
    assert len(replay_tasks_db.list_success_runs(db)) == 1


def test_migrate_skips_corrupt_json(tmp_path):
    """损坏 JSON 跳过（不阻断整批，合法条仍迁移）。"""
    runs_dir = tmp_path / "replay_runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "20260702-120000-bbcccc")
    (runs_dir / "20260703-120000-badbad.json").write_text("{not json", encoding="utf-8")
    db = str(tmp_path / "t.db")
    assert mig.migrate(str(runs_dir), db) == 1
    assert len(replay_tasks_db.list_success_runs(db)) == 1
