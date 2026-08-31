# -*- coding: utf-8 -*-
"""Phase C 提案只读 API + digest 接线测试。

历史（2026-08-31 写端点全量退役）：POST generate/review/verify/publish 四端点
与钉钉桥（dingtalk_review_bridge.py）整删，对应测试同批退役——提案引擎服务层
覆盖见 tests/research/test_proposals.py，驱动链路只剩 digest cron 直调。
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


def test_api_list_proposals(monkeypatch, tmp_path):
    """GET /proposals：列表 + status 过滤（写端点退役后本路由唯一端点）。"""
    client, db = _client(monkeypatch, tmp_path)
    proposals.create_proposal(db, change_type="A", hypothesis="h",
                              params={"min_rr": 1.8})
    r = client.get("/api/v1/research/proposals")
    assert r.status_code == 200
    rows = r.json()["proposals"]
    assert len(rows) == 1
    # status 过滤：用实际状态值过滤（不硬编码默认态字面量）
    r = client.get("/api/v1/research/proposals",
                   params={"status": rows[0]["status"]})
    assert r.status_code == 200
    assert len(r.json()["proposals"]) == 1


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
