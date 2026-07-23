# -*- coding: utf-8 -*-
"""数据机器人 brief_data 实时性段单测（Task 5 · 一期观测运营层）。

物理意图：data bot 播报需双口径——既有 mtime 健康度（被动看文件改没改），
新增内容最新日实时性（主动比交易日历期望日 vs 数据湖内容日，回答「T/T-1 到没到」）。
本测试覆盖两用例：
- 传入 freshness → 播报 markdown 含实时性段（✅ 或 ⚠️ + PASS / 日期比对）
- 不传 freshness → 向后兼容（原健康度播报不破坏，markdown 仍正常产出）
"""
from broadcast.brief_data import build_data_brief
from data.freshness import FreshnessResult


def test_brief_includes_freshness_section_when_provided():
    """传入 freshness 结果 → 播报 markdown 含实时性段。

    精确断言（Phase1 final review · 收紧三选一宽松断言）：
    - 段标题「**数据实时性**」命中（brief_data.py:67 实际渲染文案，markdown 加粗星号
      也作子串匹配的一部分——证明实时性段确实被渲染，而非靠尾部日期兜底误绿）；
    - 期望日 2026-07-23 命中（证明 FreshnessResult.expected_date 透传到文案）。
    两者同时成立才算 freshness 段真正渲染成功——原「实时性 or PASS or 日期」三选一
    会因健康度段里也有日期而误绿（健康度 head 行就含 date），收紧后消除该假阳性。
    """
    datasets = [{"key": "daily", "name": "A股日线", "status": "healthy"}]
    # FreshnessResult 字段顺序：(key, ok, latest_date, expected_date, message)
    freshness = [FreshnessResult("daily", True, "2026-07-23", "2026-07-23", "PASS")]
    result = build_data_brief("2026-07-23", datasets=datasets, freshness=freshness)
    md = result.markdown
    assert "**数据实时性**" in md and "2026-07-23" in md


def test_brief_works_without_freshness_backward_compat():
    """未传 freshness → 向后兼容（原健康度播报不破坏）。

    回归红线：freshness 为可选参数，不传时 build_data_brief 必须照常产出健康度文案，
    不引入异常、不改变既有 test_brief_data.py 两用例的断言行为。
    """
    datasets = [{"key": "daily", "name": "A股日线", "status": "healthy"}]
    result = build_data_brief("2026-07-23", datasets=datasets)
    assert result.markdown  # 仍正常产出
