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

    import sys as _sys
    import types
    import ops as _ops_pkg
    fake_gc = types.ModuleType("ops.gm_ops_common")
    fake_gc.active_legs = lambda: []
    fake_gc.runtime_config = lambda *a, **k: {}
    # 双路 patch：函数内 `from ops import gm_ops_common` 在 ops 包已挂真身属性时
    # 直接 getattr 绕过 sys.modules（全量跑时前面测试已 import 真身）——
    # 包属性与 sys.modules 必须同时换，单 patch sys.modules 只在冷进程生效
    monkeypatch.setitem(_sys.modules, "ops.gm_ops_common", fake_gc)
    monkeypatch.setattr(_ops_pkg, "gm_ops_common", fake_gc, raising=False)

    doc = nh.update()
    by_date = {d["date"]: d for d in doc["days"]}
    assert by_date[nh.ERA_START]["legs"]["main"] == 201843.0   # 旧日保住
    assert "assets" not in by_date[nh.ERA_START]               # 无 assets 源=不造数


def test_update_single_segment_log_no_crash(tmp_path, monkeypatch):
    """code-review HV-1 回归：单段日志（有可用无市值）不得 round(None) 崩溃。

    旧格式「③ 资金：nav X｜可用 Y」在 ERA 日出现时，update() 输出的 assets
    只含 available 键——缺段键不落、不炸（炸点会在 build_snapshot 中段带走
    后段全部快照文件）。
    """
    import json
    import types
    monkeypatch.setattr(nh, "LOGS", tmp_path)
    out = tmp_path / "nav_history.json"
    out.write_text(json.dumps({"base": 200000.0, "era_start": nh.ERA_START,
                               "days": []}), encoding="utf-8")
    monkeypatch.setattr(nh, "OUT", out)
    (tmp_path / f"emquant_eod_main_{nh.ERA_START}.txt").write_text(
        "③ 资金：nav 201,843｜可用 137,890", encoding="utf-8")   # 无市值段

    fake_gc = types.ModuleType("ops.gm_ops_common")
    fake_gc.active_legs = lambda: []
    fake_gc.runtime_config = lambda *a, **k: {}
    import sys as _sys
    import ops as _ops_pkg
    monkeypatch.setitem(_sys.modules, "ops.gm_ops_common", fake_gc)
    monkeypatch.setattr(_ops_pkg, "gm_ops_common", fake_gc, raising=False)

    doc = nh.update()                                          # 不抛=断言通过
    day = next(d for d in doc["days"] if d["date"] == nh.ERA_START)
    assert day["legs"]["main"] == 201843.0
    assert day["assets"]["main"] == {"available": 137890.0}    # 缺段键不落


def test_update_today_eod_overrides_stale_realtime(tmp_path, monkeypatch):
    """code-review J-5 回归：当日 EOD 终值必须能覆盖盘中实时点（盘中发布中毒）。"""
    import json
    import types
    from datetime import datetime as _dt
    monkeypatch.setattr(nh, "LOGS", tmp_path)
    out = tmp_path / "nav_history.json"
    today = f"{_dt.now():%Y-%m-%d}"
    # 场景：中午手动 publish 已把盘中值 199000 落进 today
    out.write_text(json.dumps({"base": 200000.0, "era_start": nh.ERA_START,
                               "days": [{"date": today,
                                         "legs": {"main": 199000.0},
                                         "assets": {"main": {"available": 199000.0,
                                                             "market_value": 0.0}}}]}),
                   encoding="utf-8")
    monkeypatch.setattr(nh, "OUT", out)
    # 晚间 EOD 日志生成（终值 198014）且 7002 断（active_legs 空 → 实时点不写）
    (tmp_path / f"emquant_eod_main_{today}.txt").write_text(
        "**③ 资金面**：nav **198,014**｜可用 67,899｜市值 130,115", encoding="utf-8")

    fake_gc = types.ModuleType("ops.gm_ops_common")
    fake_gc.active_legs = lambda: []
    fake_gc.runtime_config = lambda *a, **k: {}
    import sys as _sys
    import ops as _ops_pkg
    monkeypatch.setitem(_sys.modules, "ops.gm_ops_common", fake_gc)
    monkeypatch.setattr(_ops_pkg, "gm_ops_common", fake_gc, raising=False)

    doc = nh.update()
    day = next(d for d in doc["days"] if d["date"] == today)
    assert day["legs"]["main"] == 198014.0                     # EOD 覆盖盘中值
    assert day["assets"]["main"]["market_value"] == 130115.0


def test_era_start_enforced(tmp_path, monkeypatch):
    """ERA_START 边界：恰等于 era 首日的入 days，前一日的只入 pre_era。"""
    monkeypatch.setattr(nh, "LOGS", tmp_path)
    monkeypatch.setattr(nh, "OUT", tmp_path / "out.json")
    (tmp_path / f"emquant_eod_main_{nh.ERA_START}.txt").write_text(
        "nav 200,100", encoding="utf-8")
    days, pre_era = nh.parse_eod_logs()
    assert nh.ERA_START in days and days[nh.ERA_START]["main"]["nav"] == 200100.0
    assert pre_era == {}
