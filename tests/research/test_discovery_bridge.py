# -*- coding: utf-8 -*-
"""discovery 进展读取 + 自动 publish 桥单测（2026-08-03）。

物理意图：低功率 discovery 的进展要"可见 + 自动打通实验平台"——
    load_discovery_status：trial/最新 run/k 进度/新冠军/ACTIVE 实验 → digest/API；
    auto_publish_champion：新冠军 outer 优于当前 ACTIVE 的 outer 才自动 publish
    DRAFT（weight=0，promote 留人审）；不优于 → 不建 DRAFT（防垃圾候选刷屏）。
"""
import json

from research import discovery_bridge as bridge


def _seed_trial_db(tmp_path):
    """种子 discovery 库：1 个 snapshot + 2 个 trial + 1 行 search_run。"""
    from discovery.store import (connect, init_db, write_search_run,
                                 write_snapshot, write_trial)
    from discovery.snapshot import SnapshotMeta
    db = str(tmp_path / "disc.db")
    init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01", data_hash="h")
    with connect(db) as conn:
        write_snapshot(conn, meta)
        write_trial(conn, "trial_aaaa", {"window": 80, "min_rr": 2.0}, "snap1", "eng",
                    "holdout", {"ann": 0.5, "calmar": 3.5, "max_dd": 0.2, "n": 100},
                    {"ann": 0.12, "calmar": 2.0, "max_dd": 0.03, "n": 50},
                    "discovery_search")
        write_trial(conn, "trial_bbbb", {"window": 60, "min_rr": 1.5}, "snap1", "eng",
                    "holdout", {"ann": 0.6, "calmar": 4.0, "max_dd": 0.2, "n": 90},
                    {"ann": 0.05, "calmar": 1.0, "max_dd": 0.05, "n": 40},
                    "discovery_search")
        write_search_run(conn, run_id="run1", snapshot_hash="snap1", started_at="2026-08-03T15:00",
                         ended_at="2026-08-03T15:30", n_trials=2, status="budget_exhausted",
                         frontier_size=2, k_rounds_no_expansion=0, daemon_run_count=3, note="")
    return db


def test_load_discovery_status_reads_trials_and_latest_run(tmp_path):
    """进展读取：trial 数/最新 run/k 进度/前沿/新冠军 metrics 齐全。"""
    db = _seed_trial_db(tmp_path)
    st = bridge.load_discovery_status(db_path=db)
    assert st["n_trials"] == 2
    assert st["latest_run"]["run_id"] == "run1"
    assert st["latest_run"]["k_rounds_no_expansion"] == 0
    assert st["latest_run"]["daemon_run_count"] == 3
    assert st["champion"]["trial_id"] == "trial_bbbb"   # inner calmar 最高者
    assert "calmar" in st["champion"]["inner"]
    assert st["champion"]["outer"]["ann"] == 0.05


def test_load_discovery_status_empty_db(tmp_path):
    """空库/缺失 → 零值 dict（digest 渲染「—」，不抛）。"""
    st = bridge.load_discovery_status(db_path=str(tmp_path / "none.db"))
    assert st["n_trials"] == 0
    assert st["latest_run"] is None
    assert st["champion"] is None


def test_auto_publish_when_outer_better_than_active(tmp_path, monkeypatch):
    """新冠军 outer 优于 ACTIVE note 的 outer → 自动 publish DRAFT。"""
    db = _seed_trial_db(tmp_path)
    created = []

    def _fake_draft(params, source):
        created.append((params, source))
        return "neckline_auto_abc"

    monkeypatch.setattr(bridge.proposals, "_create_experiment_draft", _fake_draft)
    # ACTIVE note: outer ann=10%（低于 trial_aaaa 的 12%）→ 应 publish
    monkeypatch.setattr(bridge, "_active_outer_ann", lambda: 0.10)
    exp_id = bridge.auto_publish_champion("trial_aaaa", {"ann": 0.12}, db_path=db)
    assert exp_id == "neckline_auto_abc"
    assert created[0][0] == {"window": 80, "min_rr": 2.0}
    assert created[0][1] == "trial_aaaa"


def test_auto_publish_skips_when_outer_not_better(tmp_path, monkeypatch):
    """新冠军 outer 不优于 ACTIVE → 不 publish（防垃圾 DRAFT 刷屏）。"""
    db = _seed_trial_db(tmp_path)
    created = []
    monkeypatch.setattr(bridge.proposals, "_create_experiment_draft",
                        lambda params, source: created.append(1) or "x")
    monkeypatch.setattr(bridge, "_active_outer_ann", lambda: 0.15)   # 高于 trial_aaaa 12%
    assert bridge.auto_publish_champion("trial_aaaa", {"ann": 0.12}, db_path=db) is None
    assert created == []


def test_active_outer_ann_parses_real_experiment_db(tmp_path):
    """ACTIVE note 的 outer ann 必须能被真实解析（2026-08-04 护栏失效回归）。"""
    from experiment.models import ExperimentStatus, ExperimentVersion
    from experiment.store import create_version, init_db
    exp_db = str(tmp_path / "exp.db")
    init_db(exp_db)
    v = ExperimentVersion(
        experiment_id="neckline_disc_active", strategy_name="neckline",
        params={"min_rr": 2.0}, weight=1.0, status=ExperimentStatus.ACTIVE,
        version=1, source="discovery:x", note="outer ann=18.4% calmar=7.24",
        created_at="2026-07-25T08:00:00", activated_at="2026-07-27T07:00:00")
    create_version(exp_db, v, operator="test")
    assert bridge._active_outer_ann(db_path=exp_db) == 0.184


def test_auto_publish_guard_works_with_real_active_note(tmp_path, monkeypatch):
    """护栏回归：ACTIVE outer 可解析时，outer 不优于 ACTIVE → 绝不 publish。"""
    db = _seed_trial_db(tmp_path)
    from experiment.models import ExperimentStatus, ExperimentVersion
    from experiment.store import create_version, init_db
    exp_db = str(tmp_path / "exp.db")
    init_db(exp_db)
    v = ExperimentVersion(
        experiment_id="neckline_disc_active", strategy_name="neckline",
        params={"min_rr": 2.0}, weight=1.0, status=ExperimentStatus.ACTIVE,
        version=1, source="discovery:x", note="outer ann=18.4%",
        created_at="2026-07-25T08:00:00", activated_at="2026-07-27T07:00:00")
    create_version(exp_db, v, operator="test")
    created = []
    monkeypatch.setattr(bridge.proposals, "_create_experiment_draft",
                        lambda params, source: created.append(1) or "x")
    # 低功率 00:05 轮场景复现：outer ann=0%（评估失败/空）→ 必须被护栏拦下
    assert bridge.auto_publish_champion("trial_aaaa", {"ann": 0.0}, db_path=db) is None
    assert created == []


def test_digest_includes_discovery_progress(monkeypatch):
    """研究摘要含参数探索段（trial 数/k 进度/新冠军 outer）。"""
    from research import digest
    st = {
        "n_trials": 311,
        "latest_run": {"run_id": "run12345678", "n_trials": 1,
                       "frontier_size_prev": 7, "k_rounds_no_expansion": 0},
        "champion": {"inner": {"calmar": 14.58}, "outer": {"ann": 0.102}},
    }
    md = digest.build_digest("2026-08-03", {"n_hits": 0}, None,
                             discovery_status=st)
    assert "参数探索" in md
    assert "311" in md
    assert "k=0" in md
    assert "outer ann=10.2%" in md


def test_api_discovery_status(monkeypatch, tmp_path):
    """GET /research/discovery/status 返回进展 dict。

    P3（2026-08-13）：端点已迁 presentation/server/api/v1/discovery.py（只读 router，
    不挂 require_write）——研究进展是只读数据，不应被写权限误伤（spec §4.2）。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from presentation.server.api.v1 import discovery as discovery_api
    from research import discovery_bridge
    db = _seed_trial_db(tmp_path)
    monkeypatch.setattr(discovery_bridge, "_DISCOVERY_DB", db)
    app = FastAPI()
    app.include_router(discovery_api.router, prefix="/api/v1")
    r = TestClient(app).get("/api/v1/research/discovery/status")
    assert r.status_code == 200
    assert r.json()["n_trials"] == 2
    assert r.json()["latest_run"]["run_id"] == "run1"
