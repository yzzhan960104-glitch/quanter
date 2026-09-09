# -*- coding: utf-8 -*-
"""委员会质证工序单测（2026-09-06 · docs/research/2026-09-06-committee-embedding-design.md）。

钉死四件事：
  1. Tier 0 谱面闸纯代码规则（零 LLM）；
  2. KB 三源加载（策展 md + REJECTED 自动挖掘）；
  3. review fail-open 语义（端点异常/超时 → UNAVAILABLE，绝不炸调用方）；
  4. publish 闸唯一 fail-closed 点（ESCALATE → NEEDS_HUMAN + CommitteeEscalated；
     UNAVAILABLE/PASS → 照常 publish 带附签）。
"""
import json

import pytest

from research import proposals as P
from research.committee import kb as KB
from research.committee import review as R


# ─────────────────────── Tier 0 谱面闸（纯代码） ───────────────────────

def test_tier0_momentum_gate_param():
    hits = KB.tier0_scan({"momentum_gate": 0.15}, "收紧动量")
    assert len(hits) == 1 and "momentum_gate" in hits[0]


def test_tier0_momentum_gate_none_is_clean():
    assert KB.tier0_scan({"momentum_gate": None}, "正常参数提案") == []


def test_tier0_rejected_text_patterns():
    for word in ("MA趋势闸", "板块过滤", "MACD", "溢价分层", "动量闸"):
        assert any(word in h for h in KB.tier0_scan(None, f"建议用{word}过滤")), word


def test_tier0_posthoc_rr_pattern():
    hits = KB.tier0_scan(None, "按 rr 分层做 sizing 减配")
    assert any("事后" in h for h in hits)
    assert KB.tier0_scan(None, "收紧止损降低跳空损耗") == []


def test_tier0_embedded_in_create_proposal(tmp_path):
    db = str(tmp_path / "p.db")
    pid = P.create_proposal(db, change_type="A", hypothesis="启用动量闸过滤弱信号",
                            params={"momentum_gate": 0.5},
                            note="t")
    p = P.get_proposal(db, pid)
    assert "Tier0冲突" in (p["note"] or "")


# ─────────────────────── KB 三源加载 ───────────────────────

def test_kb_curated_topics_loaded():
    kb = KB.load_kb(refresh=True)
    for topic in ("filters_7waves", "regime_dependence", "r10_veto",
                  "posthoc_rr", "above_bull_proxy", "trailing_geometry"):
        assert topic in kb and len(kb[topic]) > 100, topic


def test_kb_auto_mines_rejected(tmp_path, monkeypatch):
    db = str(tmp_path / "p.db")
    pid = P.create_proposal(db, change_type="A", hypothesis="测试否决",
                            params={"min_rr": 1.8})
    P.mark_verifying(db, pid)
    P.mark_rejected(db, pid, note="inner 无改善")
    monkeypatch.setattr(KB, "_DB", tmp_path / "p.db")
    txt = KB._rejected_kb()
    assert pid in txt and "inner 无改善" in txt


# ─────────────────────── review fail-open / 解析 ───────────────────────

class _BoomSession:
    def __init__(self, *a, **k):
        raise RuntimeError("端点不可用（测试注入）")


def test_review_fail_open_on_endpoint_error(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMITTEE_GATE", "1")     # conftest 默认关闸，本例显式开
    monkeypatch.setattr(R, "GlmToolsSession", _BoomSession)
    monkeypatch.setattr(R, "REVIEWS_DIR", tmp_path)
    out = R.review_conclusion("t_fail", "结论：某参数收紧提升年化 5%", tier="B")
    assert out["verdict"] == "UNAVAILABLE"
    assert "端点不可用" in out["notes"] or "RuntimeError" in out["notes"]
    assert out["disclaimer"].startswith("本评审仍为假设")


class _JsonSession:
    """stub：run() 返回带合法 json 块的评审文本。"""

    def __init__(self, *a, **k):
        self.call_count = 0
        self.messages = []
        self.tool_log = []

    def run(self, user_msg, **k):
        self.call_count = 1
        return ("质证完成：数字经工具复核命中。\n```json\n"
                '{"verdict": "PASS", "fact_checks": [{"claim": "年化+5%",'
                ' "status": "命中"}], "kb_conflicts": [], '
                '"evidence_grade": "实证", "notes": "ok"}\n```')


def test_review_tier_b_parses_verdict(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMITTEE_GATE", "1")
    monkeypatch.setattr(R, "GlmToolsSession", _JsonSession)
    monkeypatch.setattr(R, "REVIEWS_DIR", tmp_path)
    out = R.review_conclusion("t_parse", "结论：收紧 stop 至 1.2 inner 年化 +5%",
                              tier="B")
    assert out["verdict"] == "PASS"
    assert out["evidence_grade"] == "实证"
    assert out["llm_calls"] == 1
    assert (tmp_path / out["artifact"].split("\\")[-1]).exists() \
        or tmp_path.joinpath(*out["artifact"].split("\\")[1:]).exists()


def test_review_bad_json_degrades_to_notes(monkeypatch, tmp_path):
    class _Bad:
        def __init__(self, *a, **k):
            self.call_count = 0
            self.messages = []
            self.tool_log = []

        def run(self, user_msg, **k):
            self.call_count = 1
            return "我忘了输出 json 块"

    monkeypatch.setattr(R, "GlmToolsSession", _Bad)
    monkeypatch.setenv("COMMITTEE_GATE", "1")
    monkeypatch.setattr(R, "REVIEWS_DIR", tmp_path)
    out = R.review_conclusion("t_bad", "结论", tier="B")
    assert out["verdict"] == "NOTES"          # 解析失败 fail-open 兜底


def test_review_gate_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMITTEE_GATE", "0")
    monkeypatch.setattr(R, "REVIEWS_DIR", tmp_path)
    out = R.review_conclusion("t_off", "结论", tier="A")
    assert out["verdict"] == "UNAVAILABLE" and out["llm_calls"] == 0


# ─────────────────────── publish 闸（唯一 fail-closed 点） ───────────────────────

def _approved(db):
    pid = P.create_proposal(db, change_type="A", hypothesis="测试 publish 闸",
                            params={"min_rr": 1.8})
    P.mark_verifying(db, pid)
    P.mark_approved(db, pid, note="t")
    return pid


def test_publish_escalate_blocks_and_transfers(tmp_path, monkeypatch):
    db = str(tmp_path / "p.db")
    pid = _approved(db)
    monkeypatch.setenv("COMMITTEE_GATE", "1")

    def _esc(db_path, proposal_id, tier="A"):
        return {"verdict": "ESCALATE", "notes": "数字与工具复核漂移",
                "artifact": "x.json", "llm_calls": 4}

    import research.committee.review as RV
    monkeypatch.setattr(RV, "review_proposal_for_publish", _esc)
    with pytest.raises(P.CommitteeEscalated):
        P.publish_proposal(db, pid)
    p = P.get_proposal(db, pid)
    assert p["status"] == "NEEDS_HUMAN"
    cj = json.loads(p["committee_json"])
    assert cj["verdict"] == "ESCALATE"


def test_publish_unavailable_fails_open(tmp_path, monkeypatch):
    db = str(tmp_path / "p.db")
    pid = _approved(db)
    monkeypatch.setenv("COMMITTEE_GATE", "1")

    def _un(db_path, proposal_id, tier="A"):
        return {"verdict": "UNAVAILABLE", "notes": "端点不可用", "llm_calls": 0}

    import research.committee.review as RV
    monkeypatch.setattr(RV, "review_proposal_for_publish", _un)
    monkeypatch.setattr(P, "_create_experiment_draft",
                        lambda params, pid: "neckline_disc_test")
    exp = P.publish_proposal(db, pid)
    assert exp == "neckline_disc_test"
    p = P.get_proposal(db, pid)
    assert p["status"] == "PUBLISHED"
    assert json.loads(p["committee_json"])["verdict"] == "UNAVAILABLE"


def test_publish_gate_off_skips_review(tmp_path, monkeypatch):
    db = str(tmp_path / "p.db")
    pid = _approved(db)
    monkeypatch.setenv("COMMITTEE_GATE", "0")

    def _never(*a, **k):                              # pragma: no cover
        raise AssertionError("闸关闭时不应触发评审")

    import research.committee.review as RV
    monkeypatch.setattr(RV, "review_proposal_for_publish", _never)
    monkeypatch.setattr(P, "_create_experiment_draft",
                        lambda params, pid: "neckline_disc_test")
    assert P.publish_proposal(db, pid) == "neckline_disc_test"
