# -*- coding: utf-8 -*-
"""training_analyzer 分析/解析单测。mock _call_glm 不真调 GLM。

Task 1.3（caisen 形态退役）：parse_review 默认 strategy_name 改 "neckline"，schema 改用
NecklineConfig（18 维）。本测试 cfg 字段对齐 NecklineConfig（min_rr / max_holding 等），
不再用 caisen 形态字段（min_rr_ratio / max_holding_bars）。
"""
import json
from unittest.mock import patch

from backtest.optimize import training_analyzer


_REPORT = {"n_hits": 12, "win_rate": 0.58, "avg_rr": 1.7, "max_drawdown": -0.14,
           "annualized_return": 0.22, "pattern_dist": {"neckline": 8}}
# NecklineConfig 字段（Task 1.3：caisen 形态 min_rr_ratio/max_holding_bars → 颈线法 min_rr/max_holding）
_CFG = {"min_rr": 1.5, "max_holding": 15}


def test_analyze_round_assembles_prompt_and_returns_report(monkeypatch):
    """正常路径：_call_glm 被调一次，入参含当前轮统计+历史，返回模型文本。

    显式注入伪 GLM key，确保走「有凭证 → 调 _call_glm」分支（mock 拦截返回值），
    不被本机/CI 环境变量缺失污染成降级分支。
    """
    monkeypatch.setenv("GLM_API_KEY", "fake-key-for-mock")
    with patch.object(training_analyzer, "_call_glm", return_value="## 第1轮报告\n表现尚可") as m:
        report = training_analyzer.analyze_round(_REPORT, _CFG, [])
    assert "第1轮报告" in report
    m.assert_called_once()
    prompt = m.call_args.args[0]
    assert "12" in prompt          # 当前轮 n_hits 进 prompt
    assert "min_rr" in prompt      # 当前 cfg 进 prompt


def test_analyze_round_degrades_without_glm_key(monkeypatch):
    """缺 GLM 凭证 → 降级返回带统计摘要的提示文本（不抛异常）。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    out = training_analyzer.analyze_round(_REPORT, _CFG, [])
    assert "降级" in out or "AI 不可用" in out
    assert "12" in out   # 仍附原始统计供人手判断


def test_parse_review_extracts_cfg_override_and_action(monkeypatch):
    """正常：GLM 返回合法 JSON → 解析出 cfg_override + action=rerun。"""
    monkeypatch.setenv("GLM_API_KEY", "fake-key-for-mock")
    glm_out = json.dumps({"cfg_override": {"min_rr": 2.0}, "action": "rerun"})
    with patch.object(training_analyzer, "_call_glm", return_value=glm_out):
        result = training_analyzer.parse_review("min_rr 提到2.0 重跑", _CFG)
    assert result["action"] == "rerun"
    assert result["cfg_override"] == {"min_rr": 2.0}


def test_parse_review_rejects_invalid_field(monkeypatch):
    """值域护栏：cfg_override 含非法字段名 → 抛 ParseError（防 GLM 幻觉改不存在的字段）。"""
    monkeypatch.setenv("GLM_API_KEY", "fake-key-for-mock")
    glm_out = json.dumps({"cfg_override": {"not_a_real_field": 1.0}, "action": "rerun"})
    with patch.object(training_analyzer, "_call_glm", return_value=glm_out):
        try:
            training_analyzer.parse_review("改某字段", _CFG)
            assert False, "应抛 ParseError"
        except training_analyzer.ParseError as e:
            assert "not_a_real_field" in str(e) or "非法" in str(e)


def test_parse_review_rejects_out_of_range(monkeypatch):
    """值域护栏：tp1_portion 超出 schema 约束(ge=0 le=1) → 抛 ParseError。"""
    monkeypatch.setenv("GLM_API_KEY", "fake-key-for-mock")
    glm_out = json.dumps({"cfg_override": {"tp1_portion": 99}, "action": "rerun"})
    with patch.object(training_analyzer, "_call_glm", return_value=glm_out):
        try:
            training_analyzer.parse_review("止盈1比例改99", _CFG)
            assert False, "应抛 ParseError（超 le=1）"
        except training_analyzer.ParseError:
            pass


def test_parse_review_degrades_on_bad_json(monkeypatch):
    """GLM 返回非 JSON → 降级抛 ParseError（loop 据此回显「没听懂」回 AWAITING_REVIEW）。"""
    monkeypatch.setenv("GLM_API_KEY", "fake-key-for-mock")
    with patch.object(training_analyzer, "_call_glm", return_value="这不是JSON"):
        try:
            training_analyzer.parse_review("说点啥", _CFG)
            assert False
        except training_analyzer.ParseError:
            pass
