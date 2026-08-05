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
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0, "t1")
    plan = {
        "confirmed": True,
        "orders": [
            {"order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
             "stop_price": 9.0, "take_profit": 12.0},
        ],
    }
    # apply_fill 写 applied_at=datetime.now()（今日）；generate_review(date) 查当日 fill，
    # date 必须取今日才能查到刚写的成交（日期口径对齐，非生产 bug 是测试口径）。
    from datetime import date as _date
    today = _date.today().isoformat()
    md = review_report.generate_review(today, plan=plan, drift=False)
    assert "交易复盘" in md
    assert "300001.SZ" in md            # 计划段 + 成交段 + 持仓段都应出现
    assert "买入 1 笔" in md            # 成交聚合
    assert "100" in md                  # 持仓 qty
    assert "无偏差" in md               # drift=False → ✅ 无偏差


def test_generate_review_empty_plan(db):
    """无计划 → 报告标「无计划」不崩。"""
    # 用无残留 plan 的未来日：generate_review(plan=None) 会 fallback load_plan(date) 读
    # 真实 logs/trading_plans；选 2099-01-01 确保无文件 → load_plan 返 None → 真无计划。
    md = review_report.generate_review("2099-01-01", plan=None, drift=None)
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


# ----------------------------------------------------------------------------
# ssot-review P2 fix：confirmed 不能只看「有 SIGNAL 行」
# ----------------------------------------------------------------------------
def test_generate_review_plan_confirmed_from_db(tmp_path, monkeypatch):
    """**ssot-review P2**：plan=None 走 DB 路径，confirmed 必须查 per-trade latest action。

    病灶：原 ``plan = {"orders": sigs, "confirmed": bool(sigs)}`` —— 有 SIGNAL 行就渲染
    「已确认」，研究员未审核的计划也会被标已确认（误导复盘决策）。

    修复后语义：confirmed = 全部 trade 的 latest action ∈ 已确认集合（is_trade_confirmed）。

    本测试种 2 个 SIGNAL-only 标的（无 CONFIRMED）→ confirmed 应为 False（渲染「待确认」）。
    """
    import json as _json
    from trading import state_store, position_book
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    # review_report 内部 from trading import state_store（lazy），patch module 默认 DB
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    account_id = "TEST_ACC_REVIEW"
    state_store.upsert_account(account_id, broker="qmt")
    date = "2099-01-02"
    # 种 2 个 SIGNAL-only（无 CONFIRMED 行）—— 研究员未审核
    for sym in ("A.SH", "B.SH"):
        tid = state_store.build_trade_id(account_id, sym, date)
        meta = _json.dumps({
            "order": {"symbol": sym, "qty": 100, "side": "buy", "price": 10.0},
            "stop_price": 9.0, "take_profit": 11.0,
            "plan_date": date, "strategy_name": "neckline", "rationale": "t",
        })
        state_store.insert_trade_event(account_id, tid, sym, "SIGNAL", meta=meta)
    # account_id 解析走 env：设 QMT_ACCOUNT_ID
    monkeypatch.setenv("QMT_ACCOUNT_ID", account_id)

    md = review_report.generate_review(date, plan=None, drift=None)
    # 核心断言：SIGNAL-only（未确认）→ 渲染「待确认」（非「已确认」）
    assert "待确认" in md, "P2 regression：未审核计划被标「已确认」（bool(sigs) 错误语义）"
    assert "已确认" not in md


def test_generate_review_plan_confirmed_true_when_all_confirmed(tmp_path, monkeypatch):
    """**ssot-review P2**：全部 CONFIRMED → confirmed=True（渲染「已确认」）。

    对照测试：补 CONFIRMED 行后 confirmed 翻 True，证明 P2 fix 不是「永远 False」。
    """
    import json as _json
    from trading import state_store, position_book
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    account_id = "TEST_ACC_REVIEW2"
    state_store.upsert_account(account_id, broker="qmt")
    date = "2099-01-02"
    for sym in ("C.SH",):
        tid = state_store.build_trade_id(account_id, sym, date)
        meta = _json.dumps({
            "order": {"symbol": sym, "qty": 100, "side": "buy", "price": 10.0},
            "stop_price": 9.0, "take_profit": 11.0,
            "plan_date": date, "strategy_name": "neckline", "rationale": "t",
        })
        state_store.insert_trade_event(account_id, tid, sym, "SIGNAL", meta=meta)
        state_store.insert_trade_event(account_id, tid, sym, "CONFIRMED")
    monkeypatch.setenv("QMT_ACCOUNT_ID", account_id)

    md = review_report.generate_review(date, plan=None, drift=None)
    assert "已确认" in md
