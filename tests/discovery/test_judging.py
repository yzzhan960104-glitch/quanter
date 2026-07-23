# -*- coding: utf-8 -*-
"""分层裁判最小版测试：L0 闸淘汰 + L1 calmar 全序。"""


def test_feasibility_gate_rejects_high_dd():
    """max_dd > 0.4 → 闸外淘汰（防极端回撤冠军，spec §3.5 L0）。"""
    from discovery.judging import feasibility_gate
    assert feasibility_gate({"max_dd": 0.5, "n": 100}) is False
    assert feasibility_gate({"max_dd": 0.4, "n": 100}) is True
    assert feasibility_gate({"max_dd": 0.3, "n": 100}) is True


def test_feasibility_gate_rejects_low_n():
    """n < 30 → 闸外淘汰（防统计意义不足的冠军）。"""
    from discovery.judging import feasibility_gate
    assert feasibility_gate({"max_dd": 0.2, "n": 10}) is False
    assert feasibility_gate({"max_dd": 0.2, "n": 30}) is True


def test_calmar_rank_filters_then_sorts():
    """先 L0 闸过滤，再按 calmar 降序全序。"""
    from discovery.judging import calmar_rank
    cands = [
        {"max_dd": 0.2, "n": 100, "calmar": 3.0},
        {"max_dd": 0.1, "n": 100, "calmar": 5.0},
        {"max_dd": 0.6, "n": 100, "calmar": 99.0},   # 闸外（max_dd>0.4）→ 剔除
        {"max_dd": 0.2, "n": 5, "calmar": 7.0},       # 闸外（n<30）→ 剔除
    ]
    ranked = calmar_rank(cands)
    assert len(ranked) == 2
    assert [c["calmar"] for c in ranked] == [5.0, 3.0]
