# -*- coding: utf-8 -*-
"""nav_history 管道（2026-09-01 可视化重构 P1）：日志解析与时代口径红线。"""
from __future__ import annotations

from pathlib import Path

from ops import nav_history as nh


def test_parse_eod_logs_era_split(tmp_path, monkeypatch):
    """双腿/旧单腿两代格式解析；ERA 前只入 pre_era 存证桶（不混画红线）。"""
    monkeypatch.setattr(nh, "LOGS", tmp_path)
    (tmp_path / "emquant_eod_2026-08-28.txt").write_text(
        "③ 资金：nav 99,752｜可用 88,637", encoding="utf-8")          # 旧单腿（10万时代）
    (tmp_path / "emquant_eod_main_2026-08-31.txt").write_text(
        "**③ 资金面**：nav **201,843**｜可用 137,890", encoding="utf-8")  # 双腿 markdown
    (tmp_path / "emquant_eod_exp_2026-08-31.txt").write_text(
        "**③ 资金面**：nav **200,000**", encoding="utf-8")
    (tmp_path / "emquant_eod_main_2026-09-01.txt").write_text(
        "nav **199,743**", encoding="utf-8")
    (tmp_path / "无关文件.txt").write_text("nav 1", encoding="utf-8")

    days, pre_era = nh.parse_eod_logs()
    assert pre_era == {"2026-08-28": 99752.0}                     # 史前存证
    assert days == {"2026-08-31": {"main": 201843.0, "exp": 200000.0},
                    "2026-09-01": {"main": 199743.0}}


def test_era_start_enforced(tmp_path, monkeypatch):
    """ERA_START 边界：恰等于 era 首日的入 days，前一日的只入 pre_era。"""
    monkeypatch.setattr(nh, "LOGS", tmp_path)
    monkeypatch.setattr(nh, "OUT", tmp_path / "out.json")
    (tmp_path / f"emquant_eod_main_{nh.ERA_START}.txt").write_text(
        "nav 200,100", encoding="utf-8")
    days, pre_era = nh.parse_eod_logs()
    assert nh.ERA_START in days and days[nh.ERA_START]["main"] == 200100.0
    assert pre_era == {}
