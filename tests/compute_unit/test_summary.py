# -*- coding: utf-8 -*-
"""summary 测试:top-N 按 inner calmar 降序 + 中文格式 + 诊断计数 + 无 ok 兜底。"""
from compute_unit.protocol import Result, TrialResult
from compute_unit.summary import summarize


def _result_with(*trials):
    return Result(task_id="t1", git_commit="a"*40, parquet_sha256="b"*64,
                  ran_at="x", results=list(trials))


def test_summarize_top_n_by_inner_calmar():
    """top-N 按 inner calmar 降序,仅 ok 的 trial。"""
    r = _result_with(
        TrialResult("low", "ok",
                    inner={"n": 10, "ann": 0.1, "calmar": 1.0, "max_dd": 0.1},
                    outer={"n": 8}, n_total=18),
        TrialResult("high", "ok",
                    inner={"n": 20, "ann": 0.3, "calmar": 5.0, "max_dd": 0.06},
                    outer={"n": 15}, n_total=35),
        TrialResult("mid", "ok",
                    inner={"n": 15, "ann": 0.2, "calmar": 3.0, "max_dd": 0.07},
                    outer={"n": 10}, n_total=25),
    )
    out = summarize(r, top_n=2)
    assert out.index("high") < out.index("mid")   # calmar5 在 calmar3 前
    assert "low" not in out                        # top-2 不含 low(calmar1)


def test_summarize_skips_failed_degenerate():
    """failed/degenerate 不进 top,末尾诊断计数展示。"""
    r = _result_with(
        TrialResult("ok1", "ok",
                    inner={"n": 5, "ann": 0.1, "calmar": 2.0, "max_dd": 0.05},
                    outer={"n": 4}, n_total=9),
        TrialResult("fail1", "failed", error="boom"),
        TrialResult("deg1", "degenerate", n_total=0),
    )
    out = summarize(r, top_n=3)
    assert "ok1" in out
    assert "fail1" not in out                       # 不进 top
    assert "failed" in out.lower() and "degenerate" in out.lower()   # 诊断计数


def test_summarize_no_ok():
    """无 ok 结果 → 提示无结果。"""
    r = _result_with(TrialResult("f", "failed", error="x"))
    out = summarize(r)
    assert "无 ok" in out
