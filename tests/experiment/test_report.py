# -*- coding: utf-8 -*-
"""report 子命令：C2d（2026-08-12）plan 归因已下沉 trading 层，本子命令仅打迁移提示。

历史：
- C2d 曾切 DB（state_store.list_signals_with_meta_by_plan_date_range），但 experiment 是
  纯配置叶子层（layer 铁律 5：禁任何 trading import，含 lazy 函数内），DB 读违反 layer，
  test_experiment_pure_leaf fail —— 故曾回退扫 plan_*.json。
- C3 删 save_plan 后 plan_*.json 不再写，report plan 聚合暂出空表。
- W0 tail（2026-08-12）：聚合下沉 trading/plan_report.py（走 DB SSoT），experiment CLI
  report 退化为 deprecation 提示，零 trading import，layer 铁律更纯。
"""
from experiment import cli


def test_report_emits_deprecation_hint(capsys, monkeypatch):
    """report 子命令保留 argparse 兼容，但仅打迁移提示，不再聚合。"""
    monkeypatch.setattr(cli, "_DEFAULT_DB", ":memory:")
    rc = cli.main(["report", "--since", "2026-07-01"])
    assert rc == 0
    out = capsys.readouterr().out
    # 迁移提示关键串（用户据此切到 trading.plan_report）
    assert "plan 归因已迁移至 trading 层" in out
    assert "trading.plan_report" in out


def test_report_no_since_still_deprecation(capsys, monkeypatch):
    """无 --since 调用也走 deprecation 分支（argparse 兼容不破）。"""
    monkeypatch.setattr(cli, "_DEFAULT_DB", ":memory:")
    rc = cli.main(["report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "plan 归因已迁移至 trading 层" in out
