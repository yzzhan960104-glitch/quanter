# -*- coding: utf-8 -*-
"""L5 publish 桥测试：discovery 冠军 trial → experiment DRAFT（spec §5.3，Plan 4 T5）。

物理意图：daemon 收敛后，冠军 trial 的 params 须沉淀为 experiment 系统的 DRAFT 候选，
供人审 promote 走既有 _eod 链路。本测试钉死两条红线：
1. publish 只建 DRAFT(weight=0)——不自动 promote（spec §2.2，防过拟合参数直冲实盘）。
2. source=discovery:<snapshot_hash[:8]>——可溯源回原 trial（审计/回滚链路）。
"""
import json


def _seed_trial(disc_db, trial_id="tid1", params=None, snapshot_hash="snap1abc"):
    """在 disc_db 预置一条 trial 行（params + snapshot_hash），供 publish 读。"""
    from discovery.store import connect, write_trial
    params = params or {"window": 5}
    with connect(disc_db) as conn:
        write_trial(conn, trial_id, params, snapshot_hash, "eng1", "holdout_2025_2026",
                    {"ann": 0.5}, {"ann": 0.4}, "tpe")


def test_publish_creates_draft(tmp_path, monkeypatch):
    """publish → experiment create_version(DRAFT, source=discovery:xxx, weight=0)。

    断言四点：status=DRAFT / weight=0 / source 前缀 discovery: / params 透传。
    experiment_id 由 publish 返回值钉死，确保调用方能拿到 id 走后续 promote。
    """
    import experiment.store as estore
    from experiment.models import ExperimentStatus
    exp_db = str(tmp_path / "e.db")
    estore.init_db(exp_db)

    import discovery.store as dstore
    disc_db = str(tmp_path / "d.db")
    dstore.init_db(disc_db)
    _seed_trial(disc_db, "tid1", {"window": 5}, "snap1abc")

    # outer 评估会调 freeze→evaluate（需 data_lake）；monkeypatch 替换 evaluate 为 stub
    # 避免依赖真实数据湖（本测只验 publish→DRAFT 桥，不验 outer 数值正确性）。
    import discovery.objective as dobj
    monkeypatch.setattr(dobj, "evaluate",
                        lambda params, universe, split: {"outer": {"ann": 0.4, "calmar": 1.5,
                                                                    "max_dd": 0.2, "n": 30}})
    import discovery.snapshot as dsnap
    monkeypatch.setattr(dsnap, "freeze",
                        lambda lake_start="2025-01-01": ({}, type("M", (), {
                            "snapshot_hash": "snap1abc", "universe_count": 0,
                            "date_range": "x~y", "lake_start": lake_start})()))

    from discovery.publish import publish_champion
    out = publish_champion("tid1", db_path=disc_db, exp_db_path=exp_db)
    # 验证 experiment 落了 DRAFT
    versions = estore.list_versions(exp_db)
    assert len(versions) == 1
    v = versions[0]
    assert v.status == ExperimentStatus.DRAFT
    assert v.weight == 0.0
    assert v.source.startswith("discovery:")
    assert v.params == {"window": 5}
    assert out["experiment_id"] == v.experiment_id
    assert out["trial_id"] == "tid1"
    assert out["snapshot_hash"] == "snap1abc"


def test_publish_no_auto_promote(tmp_path, monkeypatch):
    """publish 不自动 promote（spec §2.2，防过拟合参数直冲实盘）。

    红线：publish 只建 DRAFT（weight=0），ACTIVE 必须人审 experiment promote <id> --weight。
    若 publish 自动 ACTIVE，过拟合冠军会绕开人审直接进 _eod scan→下单，构成实盘风险。
    """
    import experiment.store as estore
    from experiment.models import ExperimentStatus
    exp_db = str(tmp_path / "e.db")
    estore.init_db(exp_db)

    import discovery.store as dstore
    disc_db = str(tmp_path / "d.db")
    dstore.init_db(disc_db)
    _seed_trial(disc_db, "tid1", {"window": 5}, "snap1abc")

    # outer 软降级路径：evaluate 抛异常时 publish 不阻断（建 DRAFT + outer=None）
    import discovery.objective as dobj
    monkeypatch.setattr(dobj, "evaluate",
                        lambda params, universe, split: (_ for _ in ()).throw(RuntimeError("boom")))
    import discovery.snapshot as dsnap
    monkeypatch.setattr(dsnap, "freeze",
                        lambda lake_start="2025-01-01": ({}, type("M", (), {
                            "snapshot_hash": "snap1abc", "universe_count": 0,
                            "date_range": "x~y", "lake_start": lake_start})()))

    from discovery.publish import publish_champion
    out = publish_champion("tid1", db_path=disc_db, exp_db_path=exp_db)
    versions = estore.list_versions(exp_db)
    assert len(versions) == 1
    # 仍是 DRAFT，未 promote ACTIVE（spec §2.2 红线）
    assert versions[0].status == ExperimentStatus.DRAFT
    assert versions[0].weight == 0.0
    # outer 软降级：evaluate 抛 → outer=None，不阻断 publish 建桥
    assert out["outer"] is None


def test_publish_unknown_trial_raises(tmp_path):
    """trial_id 不存在 → publish 抛 ValueError（fail-fast，不静默建空 DRAFT）。"""
    import experiment.store as estore
    exp_db = str(tmp_path / "e.db")
    estore.init_db(exp_db)
    import discovery.store as dstore
    disc_db = str(tmp_path / "d.db")
    dstore.init_db(disc_db)

    from discovery.publish import publish_champion
    try:
        publish_champion("unknown_tid", db_path=disc_db, exp_db_path=exp_db)
    except ValueError as e:
        assert "unknown_tid" in str(e)
    else:
        raise AssertionError("publish 未知 trial_id 应抛 ValueError（fail-fast）")
