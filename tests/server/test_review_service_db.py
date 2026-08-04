# -*- coding: utf-8 -*-
"""W3.2 完整收口（用户两轴 review）：review_service.diagnose 切 query_fills（spec §3.3.2）。

原 diagnose 无 csv_text 上传时调 export_trades（读 CSV）作复盘输入，在重放场景下
会把 24 行重复喂给 LLM。切 DB 后 export_trades 已走 fill 表真相源，diagnose 复用
export_trades 即自然切到 DB —— 本测试锁定该行为：无 csv_text 上传 → export_trades
读 DB → 1 行真相（不被 CSV 重复污染）。
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db = str(tmp_path / "ts.db")
    csv_path = str(tmp_path / "live_trades.csv")
    monkeypatch.setattr("trading.state_store._DEFAULT_DB", db)
    monkeypatch.setattr(
        "presentation.server.services.trading_service.LIVE_TRADE_LOG", csv_path)
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "db")
    from trading import state_store
    state_store.init_store(db)
    state_store.upsert_account("acct1", broker="qmt")
    return db, csv_path


def test_diagnose_reads_db_when_no_csv_text(isolated_db, monkeypatch):
    """W3.2 收口：diagnose 无 csv_text 上传 → export_trades 读 DB fill 表。

    断言：不 patch export_trades（让真函数跑），DB 1 笔 + CSV 24 行重复 →
    diagnose 拿到的 csv_text 应只含 1 行 fill（DB 真相源），不被 24 行污染。
    """
    from trading import state_store
    state_store.insert_fill(
        "oid_r1", "acct1", "20260805101000", "300001.SZ", "BUY", 100, 10.5)
    # CSV 24 行重复（事故场景）
    import csv as _csv
    db, csv_path = isolated_db
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = _csv.writer(f)
        w.writerow(["timestamp", "symbol", "direction", "shares", "price",
                    "strategy", "rationale", "kind"])
        for i in range(24):
            w.writerow([f"2026-08-05 10:00:{i:02d}", "300001.SZ", "BUY", 100,
                        10.5, "", "", "fill"])

    from presentation.server.schemas.review import ReviewRequest
    from presentation.server.services import review_service

    # 拦截 LLM 调用（不真调 GLM），捕获传给 _assemble_prompt 的 csv_text
    captured = {}

    def _fake_assemble(csv_text, *args, **kwargs):
        captured["csv_text"] = csv_text
        return "fake prompt"

    monkeypatch.setattr(review_service, "_assemble_prompt", _fake_assemble)
    monkeypatch.setattr(review_service, "get_llm_client",
                        lambda: type("FakeLLM", (), {"call": lambda self, p: "fake report"})())

    req = ReviewRequest(csv_text=None, start="2026-08-05", end="2026-08-05")
    report = review_service.diagnose(req)
    # csv_text 被 export_trades(DB) 喂入 → 只 1 行 fill（不是 24）
    assert "300001.SZ" in captured["csv_text"]
    data_lines = [ln for ln in captured["csv_text"].splitlines()[1:] if ln.strip()]
    assert len(data_lines) == 1, (
        f"diagnose 应读 DB 只 1 行 fill，实际 {len(data_lines)}（若读 CSV 24 行会污染 LLM）")
    assert report.ok is True


def test_diagnose_csv_text_upload_takes_priority(isolated_db, monkeypatch):
    """W3.2 收口：用户上传 csv_text 仍直用（spec §3.3.2 上传模式保留）。

    物理意图：复盘页面允许用户手工上传 csv_text 走复盘，此路径不查 DB/CSV，
    避免覆盖用户的定制化输入。
    """
    from presentation.server.schemas.review import ReviewRequest
    from presentation.server.services import review_service

    captured = {}

    def _fake_assemble(csv_text, *args, **kwargs):
        captured["csv_text"] = csv_text
        return "fake prompt"

    monkeypatch.setattr(review_service, "_assemble_prompt", _fake_assemble)
    monkeypatch.setattr(review_service, "get_llm_client",
                        lambda: type("FakeLLM", (), {"call": lambda self, p: "fake report"})())

    user_csv = "timestamp,symbol,direction\n2026-08-05 10:00:00,USER.SZ,BUY\n"
    req = ReviewRequest(csv_text=user_csv, start="2026-08-05", end="2026-08-05")
    review_service.diagnose(req)
    # 用户上传 csv_text 直接用（不被 DB/CSV 覆盖）
    assert captured["csv_text"] == user_csv
