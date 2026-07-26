# -*- coding: utf-8 -*-
"""review_report 单测：T 日复盘报告四段（计划/成交/持仓/对账）。

物理意图：验证 e2e 第 4 步的「可观测产物」——聚合 fill 表当日成交 + 计划 + 收盘持仓
+ drift 成 markdown，作为交易链路终点的复盘凭证。
"""
from __future__ import annotations

import pytest

from trading import position_book, review_report


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    # review_report 内部用 position_book._DEFAULT_DB 默认，patch position_book 即可联动
    position_book.init_db()
    return db_path


def test_generate_review_sections(db):
    """有计划 + 有成交 + 有持仓 + drift=False → 四段齐全。"""
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0)
    plan = {
        "confirmed": True,
        "orders": [
            {"order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
             "stop_price": 9.0, "take_profit": 12.0},
        ],
    }
    md = review_report.generate_review("2026-07-27", plan=plan, drift=False)
    assert "交易复盘" in md
    assert "300001.SZ" in md            # 计划段 + 成交段 + 持仓段都应出现
    assert "买入 1 笔" in md            # 成交聚合
    assert "100" in md                  # 持仓 qty
    assert "无偏差" in md               # drift=False → ✅ 无偏差


def test_generate_review_empty_plan(db):
    """无计划 → 报告标「无计划」不崩。"""
    md = review_report.generate_review("2026-07-27", plan=None, drift=None)
    assert "无计划" in md
    assert "未对账" in md               # drift=None → 未对账


def test_generate_review_drift_true(db):
    """drift=True → ⚠️ 有偏差。"""
    md = review_report.generate_review("2026-07-27", plan=None, drift=True)
    assert "有偏差" in md


def test_save_review_idempotent(db, tmp_path):
    """save_review 落盘 + 重复写覆盖 + 内容真是 md（三态钉死：存在 + 路径稳定 + 内容正确）。"""
    md = review_report.generate_review("2026-07-27", plan=None, drift=None)
    out = review_report.save_review("2026-07-27", md, review_dir=str(tmp_path / "reviews"))
    assert out.exists()
    # 内容真是 generate_review 的原样产物（钉死「写盘内容 == 入参 md」，防 future 改动截断/包壳）
    assert out.read_text(encoding="utf-8") == md
    out2 = review_report.save_review("2026-07-27", md, review_dir=str(tmp_path / "reviews"))
    assert out == out2  # 同一文件覆盖（路径稳定）
    assert out2.read_text(encoding="utf-8") == md  # 二次写仍内容正确
