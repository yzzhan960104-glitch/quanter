# -*- coding: utf-8 -*-
"""策略快照取数的实验中心单一真相源单测（2026-08-03 双轨治理）。

物理意图：策略播报的「参数迭代状态」原先读 legacy ``logs/param_iter_state.json``，
与实盘 experiment.db ACTIVE 分叉。本测试锁定新取数函数 ``_experiment_active_state``
优先读 experiment 库：ACTIVE 存在 → 用其版本/outer 年化；无 ACTIVE → None
（__main__ 再回退 legacy 文件并告警）。
"""
from broadcast import __main__ as bc
from experiment.models import ExperimentStatus, ExperimentVersion


def test_experiment_active_state_prefers_active_version(monkeypatch, tmp_path):
    """ACTIVE 版本 → 返回 experiment_id/version/best_annual（outer 年化从 note 解析）。"""
    db = str(tmp_path / "experiments.db")
    from experiment.store import init_db, create_version
    init_db(db)
    v = ExperimentVersion(
        experiment_id="neckline_disc_20260725_25c602", strategy_name="neckline",
        params={"min_rr": 2.0}, weight=1.0, status=ExperimentStatus.ACTIVE,
        version=1, source="discovery:77c5dc3f",
        note="outer ann=1.9% calmar=0.24 max_dd=7.8%",
        created_at="2026-07-25T08:21:37", activated_at="2026-07-27T07:20:02")
    create_version(db, v, operator="test")
    monkeypatch.setattr(bc, "_EXPERIMENT_DB", db)

    out = bc._experiment_active_state()
    assert out == {
        "experiment_id": "neckline_disc_20260725_25c602",
        "version": 1,
        "best_annual": 0.019,
    }


def test_experiment_active_state_none_when_no_active(monkeypatch, tmp_path):
    """无 ACTIVE → None（调用方回退 legacy 文件并告警）。"""
    db = str(tmp_path / "experiments.db")
    from experiment.store import init_db
    init_db(db)
    monkeypatch.setattr(bc, "_EXPERIMENT_DB", db)

    assert bc._experiment_active_state() is None
