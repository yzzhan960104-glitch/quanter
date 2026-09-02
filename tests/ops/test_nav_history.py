# -*- coding: utf-8 -*-
"""nav_history 管道（2026-09-01 可视化重构 P1；09-03 P5.2 扩展 assets 后契约重建）：
日志解析（nav/可用/市值三段）与时代口径红线。"""
from __future__ import annotations

from pathlib import Path

from ops import nav_history as nh


def test_parse_eod_logs_era_split(tmp_path, monkeypatch):
    """双腿/旧单腿两代格式解析；ERA 前只入 pre_era 存证桶（不混画红线）。

    P5.2 起 days 值为 {nav, available?, market_value?}（缺段 None 不猜）；
    旧格式「可用」段无市值 → market_value None。
    """
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
    assert days == {
        "2026-08-31": {
            "main": {"nav": 201843.0, "available": 137890.0, "market_value": None},
            "exp": {"nav": 200000.0, "available": None, "market_value": None},
        },
        "2026-09-01": {"main": {"nav": 199743.0, "available": None, "market_value": None}},
    }


def test_parse_eod_logs_full_assets(tmp_path, monkeypatch):
    """P5.2 三段齐格式：nav｜可用｜市值 全解析（资产构成回填源）。"""
    monkeypatch.setattr(nh, "LOGS", tmp_path)
    (tmp_path / "emquant_eod_main_2026-09-02.txt").write_text(
        "**③ 资金面**：nav **198,014**｜可用 67,899｜市值 130,115", encoding="utf-8")
    days, _ = nh.parse_eod_logs()
    assert days["2026-09-02"]["main"] == {
        "nav": 198014.0, "available": 67899.0, "market_value": 130115.0}


def test_update_assets_backward_compat(tmp_path, monkeypatch):
    """旧形状 nav_history.json（days[].legs 无 assets）读入合并不丢日不炸。"""
    import json
    monkeypatch.setattr(nh, "LOGS", tmp_path)
    out = tmp_path / "nav_history.json"
    out.write_text(json.dumps({
        "base": 200000.0, "era_start": nh.ERA_START,
        "days": [{"date": nh.ERA_START, "legs": {"main": 201843.0}}],
    }), encoding="utf-8")
    monkeypatch.setattr(nh, "OUT", out)

    class _FakeLeg:
        key = "main"
        label = "主腿"

    import types
    fake_gc = types.ModuleType("ops.gm_ops_common")
    fake_gc.active_legs = lambda: []
    fake_gc.runtime_config = lambda *a, **k: {}
    monkeypatch.setitem(__import__("sys").modules, "ops.gm_ops_common", fake_gc)

    doc = nh.update()
    by_date = {d["date"]: d for d in doc["days"]}
    assert by_date[nh.ERA_START]["legs"]["main"] == 201843.0   # 旧日保住
    assert "assets" not in by_date[nh.ERA_START]               # 无 assets 源=不造数


def test_era_start_enforced(tmp_path, monkeypatch):
    """ERA_START 边界：恰等于 era 首日的入 days，前一日的只入 pre_era。"""
    monkeypatch.setattr(nh, "LOGS", tmp_path)
    monkeypatch.setattr(nh, "OUT", tmp_path / "out.json")
    (tmp_path / f"emquant_eod_main_{nh.ERA_START}.txt").write_text(
        "nav 200,100", encoding="utf-8")
    days, pre_era = nh.parse_eod_logs()
    assert nh.ERA_START in days and days[nh.ERA_START]["main"]["nav"] == 200100.0
    assert pre_era == {}
