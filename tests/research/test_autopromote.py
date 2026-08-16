# -*- coding: utf-8 -*-
"""autopromote 七门量化门槛单测（T2.3 · 2026-08-16 · ADR-15）。

覆盖四块：
  ① G7 fail-closed：A3/A4 常量未定稿（None）→ evaluate_gates 直接拒跑；
  ② 七门判定逻辑：全绿 mock → all_pass；单门红（逐门翻转）→ 拒；
  ③ dry-run 零写库 / 开关关时过闸仍拒写 / 开关开时灰度两步正确调 promote/set_weight；
  ④ G3 负值 dd 口径回归（C3 同款教训——幅度 abs 后再比）。

评估链路全部 mock（evaluate_replay/evaluate_wf/neighborhood_stability/evaluate/
deflated_sharpe/freeze/resolve_champion），单测不跑真回测（真跑 30-45 分钟，
属于 T2.4 处置动作）。
"""
import pytest

from research import autopromote


@pytest.fixture
def db(tmp_path, monkeypatch):
    """临时 experiment 库：1 ACTIVE 基线 + 1 DRAFT 候选。"""
    from experiment import store, resolver
    from experiment.models import ExperimentStatus, ExperimentVersion
    p = str(tmp_path / "exp.db")
    store.init_db(p)
    store.create_version(p, ExperimentVersion(
        experiment_id="base_active", strategy_name="neckline", params={"window": 80},
        weight=1.0, status=ExperimentStatus.ACTIVE, version=1, source="discovery:x",
        created_at="2026-07-25T08:00:00", activated_at="2026-07-27T07:00:00"), operator="test")
    store.create_version(p, ExperimentVersion(
        experiment_id="cand_draft", strategy_name="neckline", params={"window": 60},
        weight=0.0, status=ExperimentStatus.DRAFT, version=2, source="research:y",
        created_at="2026-08-16T08:00:00"), operator="test")
    monkeypatch.setattr(store, "_DEFAULT_DB", p)
    monkeypatch.setattr(resolver, "_DEFAULT_DB", p)
    autopromote.A3_SURVIVES_10BPS = True          # 测试内定稿（生产常量在报告后回填）
    autopromote.A4_POS_KELLY_RATIO = 0.7
    return p


def _mock_evaluations(monkeypatch, *, mutate=None):
    """注入全绿 mock 评估链（mutate 可逐门翻转读数）。"""
    replay = {
        "base": {"inner": {"n_hits": 518, "annualized_return": 0.05,
                           "max_drawdown": -0.077},
                 "outer": {"n_hits": 611, "annualized_return": -0.29,
                           "max_drawdown": -0.212}},
        # 候选：outer ann +5.5%（≥max(-0.29+0.03, 0)=0 ✓）dd 1.4% → ratio 3.96 ≥1.5 ✓
        "cand": {"inner": {"n_hits": 150, "annualized_return": 0.052,
                           "max_drawdown": -0.026},
                 "outer": {"n_hits": 39, "annualized_return": 0.055,
                           "max_drawdown": -0.014}},
    }
    if mutate:
        mutate(replay)
    calls = {"count": 0}

    def _fake_evaluate_replay(params, universe, split, **kw):
        return replay["base"] if params.get("window") == 80 else replay["cand"]

    def _fake_evaluate_wf(params, wf_split, **kw):
        return [{"fold": "wf1", "oos": {"calmar": 1.5}}, {"fold": "wf2", "oos": {"calmar": 0.2}},
                {"fold": "wf3", "oos": {"calmar": 0.5}}, {"fold": "wf4", "oos": {"calmar": 0.1}}]

    def _fake_nb(params, universe, split, **kw):
        return {"is_plateau": True, "neighbor_mean": 3.0, "base_calmar": 4.0,
                "base_outer": {}}

    def _fake_evaluate(params, universe, split):
        return {"inner": {"sharpe": 2.0, "n": 300, "kelly": 0.21}}

    # evaluate_gates 内部函数体 lazy import 真名 → patch 源模块生效（autopromote 模块
    # 顶层无这些属性，patch autopromote.evaluate_replay 会 AttributeError）
    import discovery.objective as _obj
    monkeypatch.setattr(_obj, "evaluate_replay", _fake_evaluate_replay)
    monkeypatch.setattr(_obj, "evaluate_wf", _fake_evaluate_wf)
    monkeypatch.setattr(_obj, "evaluate", _fake_evaluate)
    import discovery.neighborhood as _nb
    monkeypatch.setattr(_nb, "neighborhood_stability", _fake_nb)
    import discovery.dsr as _dsr
    monkeypatch.setattr(_dsr, "deflated_sharpe", lambda *a, **kw: 0.9)
    import discovery.snapshot as _snap
    class _M:  snapshot_hash = "snap_x"; universe_count = 1193
    monkeypatch.setattr(_snap, "freeze", lambda *a, **kw: ({}, _M()))
    return calls


# ─────────────────────── ① G7 fail-closed ───────────────────────

def test_g7_undefined_refuses_to_run(db, monkeypatch):
    """G7 常量未定稿 → evaluate_gates 拒跑（fail-closed，红线自动恢复人审）。"""
    autopromote.A3_SURVIVES_10BPS = None
    with pytest.raises(RuntimeError, match="fail-closed"):
        autopromote.evaluate_gates("cand_draft", db_path=db)


def test_g7_a3_false_blocks(db, monkeypatch):
    """A3 结论为 False（薄边缘）→ G7 红 → all_pass=False。"""
    _mock_evaluations(monkeypatch)
    autopromote.A3_SURVIVES_10BPS = False
    res = autopromote.evaluate_gates("cand_draft", db_path=db)
    assert res["gates"]["G7_可信度前置"]["pass"] is False
    assert res["all_pass"] is False


# ─────────────────────── ② 七门判定 ───────────────────────

def test_all_gates_green(db, monkeypatch):
    """全绿 mock → all_pass=True + kelly_hat 透出。"""
    _mock_evaluations(monkeypatch)
    res = autopromote.evaluate_gates("cand_draft", db_path=db)
    assert res["all_pass"] is True
    assert res["kelly_hat"] == 0.21
    for k, g in res["gates"].items():
        if k != "_meta":
            assert g["pass"] is True, k


def test_g1_sample_size_red(db, monkeypatch):
    """G1 红：outer n=10 < 30。"""
    def mutate(r):
        r["cand"]["outer"]["n_hits"] = 10
    _mock_evaluations(monkeypatch, mutate=mutate)
    res = autopromote.evaluate_gates("cand_draft", db_path=db)
    assert res["gates"]["G1_样本量"]["pass"] is False


def test_g2_ann_negative_red(db, monkeypatch):
    """G2 红：候选 outer ann=-0.01 < 0（绝对不亏下限）。"""
    def mutate(r):
        r["cand"]["outer"]["annualized_return"] = -0.01
    _mock_evaluations(monkeypatch, mutate=mutate)
    res = autopromote.evaluate_gates("cand_draft", db_path=db)
    assert res["gates"]["G2_outer改善"]["pass"] is False


def test_g2_small_improvement_only_red(db, monkeypatch):
    """G2 红（相对改善不足）：基线 ann=0.10 时候选 0.12（+2pp<3pp）被拒——
    防「基线很好时+2pp 也放行」的门槛漂移。"""
    def mutate(r):
        r["base"]["outer"]["annualized_return"] = 0.10
        r["cand"]["outer"]["annualized_return"] = 0.12
    _mock_evaluations(monkeypatch, mutate=mutate)
    res = autopromote.evaluate_gates("cand_draft", db_path=db)
    assert res["gates"]["G2_outer改善"]["pass"] is False


def test_g3_dd_worsening_red_with_negative_convention(db, monkeypatch):
    """G3 红（C3 同款教训）：候选 dd 从 -2% 恶化到 -6%（幅度 2%→6%，劣化 4pp>2pp）——
    负值直接相减会把改善算成劣化，本门用 abs 幅度。"""
    def mutate(r):
        r["base"]["outer"]["max_drawdown"] = -0.02
        r["cand"]["outer"]["max_drawdown"] = -0.06
    _mock_evaluations(monkeypatch, mutate=mutate)
    res = autopromote.evaluate_gates("cand_draft", db_path=db)
    assert res["gates"]["G3_风险不劣化"]["pass"] is False


def test_g4_one_fold_negative_red(db, monkeypatch):
    """G4 红：wf 一折 oos calmar=-0.6（A2 教训：2025 特化参数折外塌）。"""
    def _fake_wf(params, wf_split, **kw):
        return [{"fold": "wf1", "oos": {"calmar": 1.5}}, {"fold": "wf2", "oos": {"calmar": -0.6}},
                {"fold": "wf3", "oos": {"calmar": 0.5}}, {"fold": "wf4", "oos": {"calmar": 0.1}}]
    _mock_evaluations(monkeypatch)
    import discovery.objective as _obj
    monkeypatch.setattr(_obj, "evaluate_wf", _fake_wf)
    res = autopromote.evaluate_gates("cand_draft", db_path=db)
    assert res["gates"]["G4_跨年稳健"]["pass"] is False


def test_g6_dsr_below_min_red(db, monkeypatch):
    """G6 红：DSR=0.5 < 0.8（多重比较税未缴）。"""
    _mock_evaluations(monkeypatch)
    import discovery.dsr as _dsr
    monkeypatch.setattr(_dsr, "deflated_sharpe", lambda *a, **kw: 0.5)
    res = autopromote.evaluate_gates("cand_draft", db_path=db)
    assert res["gates"]["G6_多重比较"]["pass"] is False


# ─────────────────────── ③ 写库纪律 ───────────────────────

def test_dry_run_writes_nothing(db, monkeypatch):
    """dry-run：全绿也零写库（候选仍 DRAFT、基线仍 1.0）。"""
    _mock_evaluations(monkeypatch)
    monkeypatch.setattr(autopromote, "_push", lambda md: None)
    autopromote.run("cand_draft", dry_run=True, db_path=db, notify=False)
    from experiment import store
    cand = [v for v in store.list_versions(db) if v.experiment_id == "cand_draft"][0]
    base = [v for v in store.list_versions(db) if v.experiment_id == "base_active"][0]
    assert cand.status.value == "DRAFT" and cand.weight == 0.0
    assert base.weight == 1.0


def test_switch_off_refuses_write_even_when_green(db, monkeypatch):
    """开关关：全绿 + 非 dry-run → RuntimeError 拒写（人审红线生效）。"""
    _mock_evaluations(monkeypatch)
    monkeypatch.setattr(autopromote, "_push", lambda md: None)
    monkeypatch.delenv("AUTO_PROMOTE_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="AUTO_PROMOTE_ENABLED"):
        autopromote.run("cand_draft", dry_run=False, db_path=db, notify=False)


def test_green_with_switch_promotes_two_step(db, monkeypatch):
    """开关开 + 全绿 → 灰度第一步：候选 0.3 ACTIVE + 基线 0.7；confirm → 1.0/归档。"""
    _mock_evaluations(monkeypatch)
    monkeypatch.setattr(autopromote, "_push", lambda md: None)
    monkeypatch.setenv("AUTO_PROMOTE_ENABLED", "true")
    autopromote.run("cand_draft", dry_run=False, db_path=db, notify=False)
    from experiment import store
    from experiment.models import ExperimentStatus
    cand = [v for v in store.list_versions(db) if v.experiment_id == "cand_draft"][0]
    base = [v for v in store.list_versions(db) if v.experiment_id == "base_active"][0]
    assert cand.status.value == "ACTIVE" and cand.weight == pytest.approx(0.3)
    assert base.weight == pytest.approx(0.7)
    # confirm 第二步
    autopromote.run("cand_draft", phase="confirm", dry_run=False, db_path=db, notify=False)
    cand = [v for v in store.list_versions(db) if v.experiment_id == "cand_draft"][0]
    base = [v for v in store.list_versions(db) if v.experiment_id == "base_active"][0]
    assert cand.weight == 1.0
    assert base.status.value == "ARCHIVED"
    # 审计：autopromote 操作者全程留痕
    audit_ops = {a.operator for a in store.list_audit(db)}
    assert "autopromote:gate-v1" in audit_ops


def test_not_green_never_writes(db, monkeypatch):
    """非全绿（G2 红）+ 开关开 + 非 dry-run → 只播报不写库。"""
    def mutate(r):
        r["cand"]["outer"]["annualized_return"] = -0.01
    _mock_evaluations(monkeypatch, mutate=mutate)
    monkeypatch.setattr(autopromote, "_push", lambda md: None)
    monkeypatch.setenv("AUTO_PROMOTE_ENABLED", "true")
    res = autopromote.run("cand_draft", dry_run=False, db_path=db, notify=False)
    assert res["action"] == "initial(rejected)"
    from experiment import store
    cand = [v for v in store.list_versions(db) if v.experiment_id == "cand_draft"][0]
    assert cand.status.value == "DRAFT"
