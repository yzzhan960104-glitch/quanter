# -*- coding: utf-8 -*-
"""trading.plan_report 归因聚合测试（C2d 下沉）。"""
from trading import plan_report


def test_report_aggregates_by_experiment_id(monkeypatch):
    """按 experiment_id 聚合 SIGNAL meta：订单数/末次权重/去重标的。"""
    fake_metas = [
        {"symbol": "600000.SH", "plan_date": "2026-08-11",
         "experiment_id": "exp_a", "experiment_weight": 0.6, "order": {"symbol": "600000.SH"}},
        {"symbol": "000001.SZ", "plan_date": "2026-08-11",
         "experiment_id": "exp_a", "experiment_weight": 0.6, "order": {"symbol": "000001.SZ"}},
        {"symbol": "300001.SZ", "plan_date": "2026-08-11",
         "experiment_id": "exp_b", "experiment_weight": 0.4, "order": {"symbol": "300001.SZ"}},
    ]
    monkeypatch.setattr(
        "trading.plan_report.state_store.list_signals_with_meta_by_plan_date_range",
        lambda since=None, until=None, **kw: fake_metas,
    )
    rows = plan_report.report_plan_attribution()
    by_id = {r["experiment_id"]: r for r in rows}
    assert by_id["exp_a"]["n"] == 2
    assert by_id["exp_a"]["weight"] == 0.6
    assert sorted(by_id["exp_a"]["symbols"]) == ["000001.SZ", "600000.SH"]
    assert by_id["exp_b"]["n"] == 1


def test_report_unattributed_bucket(monkeypatch):
    """缺 experiment_id 的老单归「未归因」桶，不崩。"""
    monkeypatch.setattr(
        "trading.plan_report.state_store.list_signals_with_meta_by_plan_date_range",
        lambda since=None, until=None, **kw: [
            {"symbol": "600000.SH", "plan_date": "2026-08-11",
             "order": {"symbol": "600000.SH"}}],  # 无 experiment_id
    )
    rows = plan_report.report_plan_attribution()
    assert rows[0]["experiment_id"] == "未归因"
    assert rows[0]["n"] == 1


def test_report_since_filters_by_plan_date(monkeypatch):
    """since 透传给底层 API（按 plan_date 区间，非 mtime）。"""
    captured = {}
    # brief 原文用 captured.setdefault("since", since) or [] —— setdefault 返回 since
    # （非空字符串）导致 lambda 误返回字符串而非 []，迭代按字符炸 .get。改 update 返回
    # None 让 or [] 生效，语义等价于「capture since + 返回空 meta 列表」。
    monkeypatch.setattr(
        "trading.plan_report.state_store.list_signals_with_meta_by_plan_date_range",
        lambda since=None, until=None, **kw: captured.update(since=since) or [],
    )
    plan_report.report_plan_attribution(since="2026-07-01")
    assert captured["since"] == "2026-07-01"
