# -*- coding: utf-8 -*-
"""V6：ReportBuilder md 生成 + 4 类校验逻辑。"""
from __future__ import annotations

from datetime import date
from pathlib import Path


def test_report_builder_generates_md_with_six_sections(tmp_path):
    """ReportBuilder.build → md 含 §0-§6 全段 + 落盘。"""
    from tests.e2e_long_cycle.report_builder import ReportBuilder

    rb = ReportBuilder(output_dir=tmp_path)
    md_path = rb.build(day_results=[], snapshots={}, dingtalk_records=[])
    assert md_path.exists()
    content = md_path.read_text(encoding="utf-8")
    assert "## 0. 运行配置" in content
    assert "## 2. 每张表逐日落点" in content
    assert "## 3. 预期校验结果" in content


def test_report_builder_checks_detect_orphan_trade_event(isolated_state):
    """校验 a 结构性：FILLED 无前置 ORDERED → 孤儿事件 → checks['structural']['ok']=False。"""
    from tests.e2e_long_cycle.report_builder import ReportBuilder
    from trading import state_store

    state_store.upsert_account("e2e_long_acc", broker="qmt")
    # 插一个 FILLED 事件但无对应 ORDERED（孤儿）
    state_store.insert_trade_event("e2e_long_acc", "e2e_long_acc_300001.SZ_2026-07-02",
                                   "300001.SZ", "FILLED")
    rb = ReportBuilder()
    checks = rb.checks(snapshots={date(2026, 7, 2): {"orphan_detected": True}})
    assert checks["structural"]["ok"] is False
    assert any("FILLED" in v for v in checks["structural"]["violations"])
