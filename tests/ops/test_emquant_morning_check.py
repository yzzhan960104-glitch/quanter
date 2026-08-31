# -*- coding: utf-8 -*-
"""晨检 ⑦今日计划段（2026-09-01「掘金侧计划→播报」桥 · 晨检半场）。

只测 _plan_lines 纯渲染（audit 只读解析 → 三态状态判定）；六查主体无测试
（probe 全外部依赖），维持现状不扩大测试面。
"""
from __future__ import annotations

import csv
import json

from ops import emquant_morning_check as mc


def _write_audit(path, day, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        for r in rows:
            w.writerow(r)


def test_plan_lines_three_states(tmp_path, monkeypatch):
    """三态：已成交 / 在途 / 未挂（拦截），加 skip_held/blocked 尾行。"""
    from ops import gm_ops_common as gc
    monkeypatch.setattr(gc, "GM_STRATEGY_DIR", tmp_path)
    _write_audit(tmp_path / "audit" / "audit_20260831.csv", "2026-08-31", [
        ["2026-08-31T09:31:08", "SIGNAL", json.dumps(
            {"symbol": "300803.SZ", "neckline": 84.93, "rr": 2.8})],
        ["2026-08-31T09:31:09", "SIGNAL", json.dumps(
            {"symbol": "300747.SZ", "neckline": 35.27, "rr": 2.0})],
        ["2026-08-31T09:31:10", "SIGNAL", json.dumps(
            {"symbol": "301123.SZ", "neckline": 75.0, "rr": 2.4})],
        ["2026-08-31T09:31:14", "ORDER_PLACED", json.dumps(
            {"symbol": "300803.SZ", "qty": 100, "price": 96.02})],
        ["2026-08-31T09:31:14", "ORDER_PLACED", json.dumps(
            {"symbol": "300747.SZ", "qty": 300, "price": 42.78})],
        ["2026-08-31T09:31:32", "POS_ENRICHED", json.dumps({"symbol": "300803.SZ"})],
        ["2026-08-31T09:31:12", "SIGNAL_SKIP_HELD", "{}"],
        ["2026-08-31T09:31:15", "ORDER_BLOCKED", "{\"reason\": \"定尺不足一手\"}"],
    ])
    lines = mc._plan_lines("2026-08-31")
    text = "\n".join(lines)
    assert "**⑦ 今日计划**" in text
    # 已成交：SIGNAL + ORDER_PLACED + POS_ENRICHED
    assert "300803.SZ ×100 @ 96.02" in text and "✅ 已成交" in text
    # 在途：有挂单无成交
    assert "300747.SZ ×300 @ 42.78" in text and "⏳ 在途" in text
    # 未挂：仅 SIGNAL
    assert "301123.SZ" in text and "未挂（拦截/额度）" in text
    # 颈线/RR 跟行
    assert "颈线 84.93 · RR 2.8" in text
    # 尾行：skip_held + blocked
    assert "已持有跳过 1" in text and "拦截 1" in text


def test_plan_lines_empty_day(tmp_path, monkeypatch):
    """缺 audit 文件=未扫描/非交易日 → 「无新信号」占位不炸。"""
    from ops import gm_ops_common as gc
    monkeypatch.setattr(gc, "GM_STRATEGY_DIR", tmp_path)
    lines = mc._plan_lines("2026-08-30")
    assert any("今日无新信号" in ln for ln in lines)


def _facts(sigs_placed_filled):
    """构造 _scan_facts 等价 dict：[(sym, placed_detail, filled)]。"""
    facts = {"signals": [], "placed": {}, "filled": set(),
             "skip_held": 0, "blocked": 0}
    for sym, pl, filled in sigs_placed_filled:
        facts["signals"].append({"symbol": sym, "neckline": 10.0, "rr": 2.0})
        if pl:
            facts["placed"][sym] = pl
        if filled:
            facts["filled"].add(sym)
    return facts


def _write_preview_artifact(root_logs, plan_date, plan):
    import json
    root_logs.mkdir(parents=True, exist_ok=True)
    (root_logs / f"plan_preview_{plan_date}.json").write_text(
        json.dumps({"stamp": "t", "plan_date": plan_date, "plan": plan}),
        encoding="utf-8")


def test_preview_recon_full_match(tmp_path, monkeypatch):
    """全一致：集合/量/价（1 分容差内）→ N/N ✅。"""
    monkeypatch.setattr(mc, "ROOT", tmp_path)
    _write_preview_artifact(tmp_path / "logs", "2026-09-01",
                            [{"sym": "300657.SZ", "qty": 300, "entry": 43.3408},
                             {"sym": "300602.SZ", "qty": 200, "entry": 53.32}])
    facts = _facts([("300657.SZ", {"qty": 300, "price": 43.35}, True),   # 钳价取整边界容差内
                    ("300602.SZ", {"qty": 200, "price": 53.32}, False)])
    lines = mc._preview_recon_lines("2026-09-01", facts)
    assert any("2/2 一致 ✅" in ln for ln in lines)


def test_preview_recon_diffs(tmp_path, monkeypatch):
    """三类漂移逐项列明：预演有实无 / 实有预演无 / 价量漂移。"""
    monkeypatch.setattr(mc, "ROOT", tmp_path)
    _write_preview_artifact(tmp_path / "logs", "2026-09-01",
                            [{"sym": "300657.SZ", "qty": 300, "entry": 43.34},
                             {"sym": "300602.SZ", "qty": 200, "entry": 53.32},
                             {"sym": "300182.SZ", "qty": 2600, "entry": 5.73}])
    facts = _facts([("300657.SZ", {"qty": 100, "price": 43.34}, True),   # 量漂移
                    ("688111.SH", {"qty": 100, "price": 30.0}, False)])  # 实有预演无
    lines = mc._preview_recon_lines("2026-09-01", facts)
    text = "\n".join(lines)
    assert "0/4" in text                       # 一致 0（300657 计入漂移）/ 并集 4
    assert "预演有·实无：300602.SZ" in text and "预演有·实无：300182.SZ" in text
    assert "实有·预演无：688111.SH" in text
    assert "价量漂移：300657.SZ 预演×300@43.34 实×100@43.34" in text


def test_preview_recon_missing_artifact(tmp_path, monkeypatch):
    """缺档案降级占位不炸（预演未部署期/非交易日）。"""
    monkeypatch.setattr(mc, "ROOT", tmp_path)
    lines = mc._preview_recon_lines("2026-09-01", _facts([]))
    assert any("无昨晚档案" in ln for ln in lines)
