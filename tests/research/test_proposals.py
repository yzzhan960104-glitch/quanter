# -*- coding: utf-8 -*-
"""Phase C 研究提案工作流单测（2026-08-03）。

物理意图：Agent 长周期多轮交互的数据地基——Agent 基于 digest/历史生成结构化
实验提案（A 参数档 / B 过滤器开关 / C 结构进化），A 档自动验证（回测 + 门槛），
钉钉人审批准后 publish 到 experiment DRAFT（promote 仍留人审）。本测试钉死：
    表/状态机 / verify 门槛（基线对比）/ LLM 生成 / 审核解析 / publish 桥。
"""
import json

from research import proposals


def _sample_params():
    return {"window": 80, "min_rr": 2.0, "max_holding": 18}   # 基线（ACTIVE 风格）


def _proposal_params():
    return {"window": 80, "min_rr": 1.8, "max_holding": 18}   # 提案（下调 min_rr）


def _mk_proposal(db, change_type="A", params=None):
    return proposals.create_proposal(
        db, change_type=change_type, hypothesis="放宽 min_rr 试更少但更优的信号",
        params=params if params is not None else _proposal_params(),
        expected_effect="inner 胜率提升", risk="样本减少", note="test")


def _fake_eval_replay(factory):
    """构造 evaluate_replay 桩：按 params 返回 inner/outer 指标。"""
    def _f(params, universe, split):
        return factory(params)
    return _f


def test_create_list_get_and_state_machine(tmp_path):
    """建提案 → 列表/读取 → 状态迁移（PENDING→VERIFYING→APPROVED/REJECTED）。"""
    db = str(tmp_path / "p.db")
    pid = _mk_proposal(db)
    assert pid.startswith("p_")
    rows = proposals.list_proposals(db)
    assert len(rows) == 1 and rows[0]["status"] == "PENDING"
    p = proposals.get_proposal(db, pid)
    assert p["hypothesis"].startswith("放宽")
    assert json.loads(p["params_json"]) == _proposal_params()

    proposals.mark_verifying(db, pid)
    proposals.mark_approved(db, pid, note="通过")
    assert proposals.get_proposal(db, pid)["status"] == "APPROVED"
    proposals.mark_published(db, pid, experiment_id="neckline_disc_test")
    assert proposals.get_proposal(db, pid)["status"] == "PUBLISHED"


def test_verify_approves_when_inner_better_and_outer_not_worse(tmp_path, monkeypatch):
    """A 档门槛：inner 改善且 outer 不显著劣化 → APPROVED + verification_json。"""
    db = str(tmp_path / "p.db")
    pid = _mk_proposal(db)
    baseline = {
        "inner": {"n_hits": 200, "win_rate": 0.40, "avg_rr": 1.2,
                  "max_drawdown": -0.10, "annualized_return": 0.15},
        "outer": {"n_hits": 100, "win_rate": 0.35, "avg_rr": 1.0,
                  "max_drawdown": -0.12, "annualized_return": 0.08},
    }
    prop = {
        "inner": {"n_hits": 180, "win_rate": 0.45, "avg_rr": 1.5,
                  "max_drawdown": -0.08, "annualized_return": 0.22},
        "outer": {"n_hits": 90, "win_rate": 0.34, "avg_rr": 1.0,
                  "max_drawdown": -0.13, "annualized_return": 0.06},
    }
    calls = {}

    def _factory(params):
        return baseline if params.get("min_rr") == 2.0 else prop

    # 基线=ACTIVE（resolve_active 桩）；提案=提案 params（含 min_rr=1.8）
    from experiment.models import ActiveExperiment
    monkeypatch.setattr(
        proposals, "resolve_active",
        lambda: [ActiveExperiment("exp1", "neckline", _sample_params(), 1.0, "2026-07-27")])
    monkeypatch.setattr(proposals, "evaluate_replay", _fake_eval_replay(_factory))
    monkeypatch.setattr(proposals, "freeze", lambda lake_start="2025-01-01": ({}, None))

    ok = proposals.verify_proposal(db, pid)
    assert ok is True
    p = proposals.get_proposal(db, pid)
    assert p["status"] == "APPROVED"
    ver = json.loads(p["verification_json"])
    assert ver["verdict"] == "approved"
    assert ver["baseline"]["win_rate"] == 0.40
    assert ver["proposal"]["win_rate"] == 0.45


def test_verify_rejects_when_inner_worse(tmp_path, monkeypatch):
    """A 档门槛：inner 劣化 → REJECTED（不浪费人审）。"""
    db = str(tmp_path / "p.db")
    pid = _mk_proposal(db)
    baseline = {
        "inner": {"n_hits": 200, "win_rate": 0.40, "avg_rr": 1.2,
                  "max_drawdown": -0.10, "annualized_return": 0.15},
        "outer": {"n_hits": 100, "win_rate": 0.35, "avg_rr": 1.0,
                  "max_drawdown": -0.12, "annualized_return": 0.08},
    }
    prop = {
        "inner": {"n_hits": 50, "win_rate": 0.32, "avg_rr": 1.0,
                  "max_drawdown": -0.12, "annualized_return": 0.05},
        "outer": {"n_hits": 20, "win_rate": 0.30, "avg_rr": 0.9,
                  "max_drawdown": -0.15, "annualized_return": 0.02},
    }
    from experiment.models import ActiveExperiment
    monkeypatch.setattr(
        proposals, "resolve_active",
        lambda: [ActiveExperiment("exp1", "neckline", _sample_params(), 1.0, "2026-07-27")])
    monkeypatch.setattr(proposals, "evaluate_replay",
                        _fake_eval_replay(lambda params: baseline if params.get("min_rr") == 2.0 else prop))
    monkeypatch.setattr(proposals, "freeze", lambda lake_start="2025-01-01": ({}, None))

    ok = proposals.verify_proposal(db, pid)
    assert ok is False
    p = proposals.get_proposal(db, pid)
    assert p["status"] == "REJECTED"
    assert json.loads(p["verification_json"])["verdict"] == "rejected"


def test_judge_dd_sign_convention_regression():
    """C3 实弹回归（2026-08-16）：负值 dd 口径下改善不得误判为劣化。

    真实案例 p_553e383d：提案 outer dd -1.4% vs 基线 -21.2%——旧代码
    dd_worse=(-0.014)-(-0.212)=+19.8% 触发误拒；真劣化（-0.10 vs -0.08）反而算出
    负数放行。abs 幅度归一后两种符号口径语义都正确。
    """
    # ① C3 实弹数字：大幅改善 → 必须放行（不被 dd 闸拦截）
    baseline = {"inner": {"n_hits": 518, "win_rate": 0.446, "avg_rr": 0.737,
                          "annualized_return": 0.05},
                "outer": {"max_drawdown": -0.212, "annualized_return": -0.293}}
    prop = {"inner": {"n_hits": 67, "win_rate": 0.567, "avg_rr": 0.274,
                      "annualized_return": 0.052},
            "outer": {"max_drawdown": -0.014, "annualized_return": 0.055}}
    ok, reason = proposals._judge(baseline, prop)
    assert ok is True, reason   # inner 胜率 +12pp 改善 + outer dd 大幅改善 → APPROVED

    # ② 真劣化 8pp（-0.10 → -0.18，幅度 0.10→0.18）→ 必须被 dd 闸拒绝（旧代码放行）
    baseline2 = {"inner": {"n_hits": 200, "win_rate": 0.40, "avg_rr": 1.2,
                           "annualized_return": 0.15},
                 "outer": {"max_drawdown": -0.10, "annualized_return": 0.08}}
    prop2 = {"inner": {"n_hits": 180, "win_rate": 0.45, "avg_rr": 1.5,
                       "annualized_return": 0.22},
             "outer": {"max_drawdown": -0.18, "annualized_return": 0.06}}
    ok2, reason2 = proposals._judge(baseline2, prop2)
    assert ok2 is False and "回撤" in reason2

    # ③ 正值口径（discovery metrics_of）同样正确：0.12 → 0.13 仅 1pp，不拒
    baseline3 = {"inner": {"n_hits": 200, "win_rate": 0.40, "avg_rr": 1.2,
                           "annualized_return": 0.15},
                 "outer": {"max_drawdown": 0.12, "annualized_return": 0.08}}
    prop3 = {"inner": {"n_hits": 180, "win_rate": 0.45, "avg_rr": 1.5,
                       "annualized_return": 0.22},
             "outer": {"max_drawdown": 0.13, "annualized_return": 0.06}}
    ok3, _ = proposals._judge(baseline3, prop3)
    assert ok3 is True


def test_rejected_proposal_can_be_reverified(tmp_path, monkeypatch):
    """重裁决口（C3）：REJECTED → VERIFYING 合法——dd bug 修复后误拒提案可重跑。"""
    db = str(tmp_path / "p.db")
    pid = _mk_proposal(db)
    proposals.mark_verifying(db, pid)
    proposals.mark_rejected(db, pid)
    proposals.mark_verifying(db, pid)     # 旧状态机此处抛 ValueError
    assert proposals.get_proposal(db, pid)["status"] == "VERIFYING"


def test_generate_proposal_uses_llm_and_schema_guard(tmp_path, monkeypatch):
    """LLM 生成提案：合法 JSON + 值域护栏；GLM 不可用 → None（不落库）。"""
    db = str(tmp_path / "p.db")
    calls = []

    def _fake_call(prompt):
        calls.append(prompt)
        return json.dumps({
            "change_type": "A",
            "hypothesis": "min_rr 提到 1.8 过滤弱信号",
            "params": {"min_rr": 1.8},
            "expected_effect": "胜率提升",
            "risk": "样本减少",
        }, ensure_ascii=False)

    monkeypatch.setattr(proposals, "get_llm_client",
                        lambda: type("C", (), {"call": staticmethod(_fake_call)})())
    pid = proposals.generate_proposal(db, "# 研究摘要", [])
    assert pid is not None
    p = proposals.get_proposal(db, pid)
    assert p["change_type"] == "A"
    assert json.loads(p["params_json"]) == {"min_rr": 1.8}
    assert "min_rr" in calls[0]

    # GLM 不可用 → None
    monkeypatch.setattr(proposals, "get_llm_client",
                        lambda: (_ for _ in ()).throw(RuntimeError("no key")))
    assert proposals.generate_proposal(db, "# 研究摘要", []) is None


def test_parse_and_submit_review(tmp_path):
    """钉钉审核解析：通过/否决 proposal_xxx → 状态迁移（规则解析，不依赖 LLM）。"""
    db = str(tmp_path / "p.db")
    pid = _mk_proposal(db)
    assert proposals.parse_review(f"通过 {pid}") == {"action": "approve", "proposal_id": pid}
    assert proposals.parse_review(f"否决 {pid} 理由样本少") == {"action": "reject", "proposal_id": pid}
    assert proposals.parse_review("随便聊聊") is None

    proposals.mark_verifying(db, pid)
    proposals.submit_review(db, f"通过 {pid}")
    assert proposals.get_proposal(db, pid)["status"] == "APPROVED"


def test_publish_creates_experiment_draft(tmp_path, monkeypatch):
    """APPROVED → publish 到 experiment DRAFT（create_version 桥），promote 仍留人审。"""
    db = str(tmp_path / "p.db")
    pid = _mk_proposal(db)
    created = []
    monkeypatch.setattr(
        proposals, "_create_experiment_draft",
        lambda params, source: created.append((params, source)) or "neckline_disc_abc")
    proposals.mark_verifying(db, pid)
    proposals.mark_approved(db, pid)
    exp_id = proposals.publish_proposal(db, pid)
    assert exp_id == "neckline_disc_abc"
    assert proposals.get_proposal(db, pid)["status"] == "PUBLISHED"
    assert created[0][0] == _proposal_params()


def test_create_experiment_draft_idempotent_same_source(tmp_path, monkeypatch):
    """C4 回归（2026-08-16 06:08 实弹）：同 source 重复建 DRAFT 撞 UNIQUE → daemon 整轮 crash。

    期望：第二次调用幂等返回既有 experiment_id（语义级守卫①），库里同 source 仅一版。
    """
    from experiment import store as estore
    exp_db = str(tmp_path / "exp.db")
    monkeypatch.setattr(estore, "_DEFAULT_DB", exp_db)   # 惰性 import 于函数体内，patch 模块属性即生效
    params = {"min_rr": 2.0}
    id1 = proposals._create_experiment_draft(params, "trial_aaaa12")
    id2 = proposals._create_experiment_draft(params, "trial_aaaa12")   # daemon 第二轮同冠军
    assert id1 == id2
    same = [v for v in estore.list_versions(exp_db)
            if v.source == "research_proposal:trial_aaaa12"]
    assert len(same) == 1


def test_create_experiment_draft_tail6_collision_creates_second_version(tmp_path, monkeypatch):
    """C4 窄口径守卫②（review 修复 2026-08-16）：不同 source 但尾 6 位相同 + 同日撞 id
    → 自动加序号后缀建第二版，**不再静默返回第一个 id**——原实现会把第二提案
    mark_published 到别人的 experiment_id 上，params 从未入库，证据链错配。
    """
    from experiment import store as estore
    exp_db = str(tmp_path / "exp.db")
    monkeypatch.setattr(estore, "_DEFAULT_DB", exp_db)
    id1 = proposals._create_experiment_draft({"min_rr": 2.0}, "disc_aaaa12")
    id2 = proposals._create_experiment_draft({"min_rr": 1.5}, "prop_aaaa12")  # 尾6位同为 aaaa12
    assert id1 != id2 and id2.endswith("-2")
    versions = estore.list_versions(exp_db)
    assert len(versions) == 2
    assert {v.params["min_rr"] for v in versions} == {2.0, 1.5}   # 两提案参数各自入库
