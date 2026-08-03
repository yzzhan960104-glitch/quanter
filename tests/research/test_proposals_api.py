# -*- coding: utf-8 -*-
"""Phase C 提案 API + 钉钉桥 + digest 接线测试（2026-08-03）。

物理意图：提案工作流必须能经 HTTP/钉钉驱动（Agent 交互的长周期载体）——
list/generate/verify/review/publish 五端点 + bridge 路由（含 proposal 关键词走
research/review）+ digest --proposals 每日自动生成。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from presentation.server.api.v1 import research as research_api
from research import proposals


def _client(monkeypatch, tmp_path):
    """最小 app + monkeypatch 默认 DB 指向 tmp（不碰生产库）。"""
    db = str(tmp_path / "proposals.db")
    monkeypatch.setattr(proposals, "_DEFAULT_DB", db)
    app = FastAPI()
    app.include_router(research_api.router, prefix="/api/v1")
    return TestClient(app), db


def test_api_generate_list_review_publish(monkeypatch, tmp_path):
    """全链路：generate → list → review 通过 → publish → experiment DRAFT。"""
    client, db = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(proposals, "get_llm_client", lambda: type(
        "C", (), {"call": staticmethod(lambda prompt: (
            '{"change_type":"A","hypothesis":"h","params":{"min_rr":1.8},'
            '"expected_effect":"e","risk":"r"}'))})())

    r = client.post("/api/v1/research/proposals/generate", json={"digest": "# d", "history": []})
    assert r.status_code == 200
    pid = r.json()["proposal_id"]
    assert pid

    r = client.get("/api/v1/research/proposals")
    assert r.status_code == 200
    assert len(r.json()["proposals"]) == 1

    r = client.post("/api/v1/research/proposals/review", json={"text": f"通过 {pid}"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["status"] == "APPROVED"

    monkeypatch.setattr(proposals, "_create_experiment_draft",
                        lambda params, source: "neckline_disc_abc")
    r = client.post(f"/api/v1/research/proposals/{pid}/publish")
    assert r.status_code == 200
    assert r.json()["experiment_id"] == "neckline_disc_abc"
    assert proposals.get_proposal(db, pid)["status"] == "PUBLISHED"


def test_api_verify_endpoint(monkeypatch, tmp_path):
    """POST /verify：自动验证 A 档（门槛判定由 proposals.verify_proposal 承担）。"""
    client, db = _client(monkeypatch, tmp_path)
    pid = proposals.create_proposal(
        db, change_type="A", hypothesis="h", params={"min_rr": 1.8})
    def _fake_verify(db_path, proposal_id, lake_start="2025-01-01"):
        proposals.mark_verifying(db_path, proposal_id)
        proposals.mark_approved(db_path, proposal_id)
        return True

    monkeypatch.setattr(proposals, "verify_proposal", _fake_verify)
    r = client.post(f"/api/v1/research/proposals/{pid}/verify?lake_start=2025-01-01")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "status": "APPROVED"}


def test_dingtalk_bridge_routes_proposal_text_to_research(monkeypatch, tmp_path):
    """bridge：@文本含提案 id → POST /api/v1/research/review（不再走 training）。"""
    import infra.tools.dingtalk_review_bridge as bridge
    calls = []

    class _FakeResp:
        def read(self):
            return b'{"ok": true, "proposal_id": "p_12345678"}'

    class _FakeUrlopen:
        def __init__(self, req, timeout=10):
            calls.append(req.full_url)

        def __enter__(self):
            return _FakeResp()

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(bridge.urllib.request, "urlopen", _FakeUrlopen)
    bridge.main(["通过 p_12345678"])
    assert calls and "/api/v1/research/proposals/review" in calls[0]


def test_digest_main_with_proposals_appends_proposal(monkeypatch, tmp_path):
    """digest --proposals：生成提案后追加到 markdown（钉钉同步内容含提案）。"""
    from research import digest
    monkeypatch.setattr(digest, "load_live_fills", lambda *a, **k: [])
    monkeypatch.setattr(digest, "summarize_fills", lambda fills: {"n_hits": 0})
    monkeypatch.setattr(digest, "load_backtest_expectation", lambda **k: None)
    monkeypatch.setattr(digest, "load_live_perf_from_state_store", lambda **k: {})
    monkeypatch.setattr(digest, "build_digest", lambda *a, **k: "MD")
    monkeypatch.setattr(digest.proposals, "generate_proposal",
                        lambda db_path, digest_md, history, max_pending=2: "p_abc123")
    monkeypatch.setattr(digest.proposals, "get_proposal",
                        lambda db_path, pid: {
                            "hypothesis": "假设", "params_json": '{"min_rr": 1.8}',
                            "expected_effect": "效果", "risk": "风险"})
    out_path = str(tmp_path / "digest.md")
    md = digest.main(["--proposals", "--out", out_path])
    assert "p_abc123" in md
