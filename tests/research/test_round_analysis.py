# -*- coding: utf-8 -*-
"""round_analysis（T3.1）+ digest 归因注入 + 学习回路（T3.3）单测。"""
import json

from research import round_analysis


def _trades():
    """4 笔样例：2 止盈（+1.5/+2.5）、1 止损（-3.0）、1 超时（-0.5），跨两月。"""
    return [
        {"symbol": "300001.SZ", "exit_reason": "tp2", "avg_pnl_pct": 1.5,
         "signal_date": "2026-06-10"},
        {"symbol": "300002.SZ", "exit_reason": "tp2", "avg_pnl_pct": 2.5,
         "signal_date": "2026-06-20"},
        {"symbol": "300003.SZ", "exit_reason": "stop_loss", "avg_pnl_pct": -3.0,
         "signal_date": "2026-07-05"},
        {"symbol": "300004.SZ", "exit_reason": "timeout", "avg_pnl_pct": -0.5,
         "signal_date": "2026-07-15"},
    ]


def test_analyze_trades_reason_and_monthly():
    """exit_reason/月度聚合：n/胜率/均笔/累计 四数齐全。"""
    a = round_analysis.analyze_trades(_trades())
    assert a["n"] == 4
    tp = a["exit_reason"]["tp2"]
    assert tp["n"] == 2 and tp["win_rate"] == 1.0 and tp["avg_pnl"] == 2.0
    sl = a["exit_reason"]["stop_loss"]
    assert sl["win_rate"] == 0.0 and sl["avg_pnl"] == -3.0
    assert a["monthly"]["2026-06"]["total_pnl"] == 4.0
    assert a["monthly"]["2026-07"]["n"] == 2
    assert a["regime"] is None          # 未注入分类器 → 跳过该维度


def test_analyze_trades_with_regime_injected():
    """regime 分类器注入：按日期分组聚合。"""
    def _cls(d):
        return "BEAR" if d and d[:7] == "2026-07" else "BULL"
    a = round_analysis.analyze_trades(_trades(), regime_classify=_cls)
    assert a["regime"]["BULL"]["n"] == 2
    assert a["regime"]["BEAR"]["total_pnl"] == -3.5


def test_analyze_trades_empty():
    """空 trades → 零结构（调用方降级），不抛。"""
    a = round_analysis.analyze_trades([])
    assert a["n"] == 0 and a["exit_reason"] == {}


def test_render_analysis_md():
    """markdown 渲染：三表头齐全；空数据返回空串。"""
    md = round_analysis.render_analysis_md(
        round_analysis.analyze_trades(_trades(), regime_classify=lambda d: "BULL"))
    assert "逐笔归因" in md and "按出场原因" in md and "按月度" in md and "按市场状态" in md
    assert "stop_loss" in md and "-3.00" in md
    assert round_analysis.render_analysis_md({"n": 0}) == ""


def test_digest_renders_analysis_section():
    """digest 注入：analysis_md 非空 → 归因段渲染在「数据与实验状态」之前。"""
    from research import digest
    md = digest.build_digest("2026-08-16", {"n_hits": 0}, None,
                             analysis_md="### 逐笔归因（n=4）\n| x |\n|---|")
    assert "逐笔归因" in md
    assert md.index("逐笔归因") < md.index("数据与实验状态")


def test_generate_proposal_prompt_contains_rejection_reason(monkeypatch, tmp_path):
    """T3.3 学习回路：REJECTED 提案的理由出现在下轮 prompt（防同形状反复试错）。"""
    from research import proposals
    calls = []

    def _fake_call(prompt):
        calls.append(prompt)
        return json.dumps({"change_type": "A", "hypothesis": "h",
                           "params": {"min_rr": 1.2},
                           "expected_effect": "e", "risk": "r"})

    monkeypatch.setattr(proposals, "get_llm_client",
                        lambda: type("C", (), {"call": staticmethod(_fake_call)})())
    db = str(tmp_path / "p.db")
    # 历史一条 REJECTED（verification_json 带理由）
    hist = [{"proposal_id": "p_dead0001", "status": "REJECTED",
             "params_json": json.dumps({"min_rr": 1.5}), "note": "",
             "verification_json": json.dumps(
                 {"reason": "outer 年化 -40% 低于下限"})}]
    proposals.generate_proposal(db, "# 摘要", hist)
    assert any("outer 年化 -40% 低于下限" in p for p in calls)
    assert any("[REJECTED] p_dead0001" in p for p in calls)
