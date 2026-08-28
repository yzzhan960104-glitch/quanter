# -*- coding: utf-8 -*-
"""掘金日终播报：audit 漏斗统计 + gm→ts 符号折算 + 报告组装（钉钉对齐 2026-08-28）。"""
from __future__ import annotations

import csv
import json

from ops import emquant_eod_report as rpt


def _write_audit(path, day, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        for r in rows:
            w.writerow(r)


def test_audit_stats_funnel(tmp_path, monkeypatch):
    from ops import gm_ops_common as gc
    monkeypatch.setattr(gc, "GM_STRATEGY_DIR", tmp_path)
    _write_audit(tmp_path / "audit" / "audit_20260828.csv", "2026-08-28", [
        ["2026-08-28T09:31:08", "SIGNAL", json.dumps({"symbol": "300456.SZ"})],
        ["2026-08-28T09:31:15", "WARN", json.dumps({"type": "buy_price_clamped", "clamped_to": 10.51})],
        ["2026-08-28T09:31:15", "ORDER_PLACED", json.dumps({"symbol": "300420.SZ", "repair_of": "old1"})],
        ["2026-08-28T09:31:15", "ORDER_BLOCKED", "{\"reason\": \"定尺不足一手（...）\"}"],
        ["2026-08-28T09:31:15", "ORDER_BLOCKED", "{\"reason\": \"单日有效新挂已达试点上限 2\"}"],
        ["2026-08-28T09:31:31", "POS_ENRICHED", json.dumps({"symbol": "300420.SZ"})],
        ["2026-08-28T15:36:00", "EOD", json.dumps({"effective_today": 2, "open_orders": 0})],
    ])
    st = rpt._audit_stats("2026-08-28")
    assert st["signals"] == 1 and st["placed"] == 1 and st["fills"] == 1
    assert st["repaired"] == 1 and st["clamped"] == 1
    assert st["blocked"]["定尺不足一手"] == 1 and st["blocked"]["单日上限"] == 1
    assert st["eod"]["effective_today"] == 2
    empty = rpt._audit_stats("2026-08-01")            # 缺文件=全零不炸
    assert empty["signals"] == 0 and empty["eod"] is None


def test_gm_symbol_to_ts():
    assert rpt._gm_symbol_to_ts("SZSE.300433") == "300433.SZ"
    assert rpt._gm_symbol_to_ts("SHSE.688800") == "688800.SH"


def test_build_report_renders(tmp_path, monkeypatch):
    from ops import gm_ops_common as gc
    monkeypatch.setattr(gc, "GM_STRATEGY_DIR", tmp_path)
    monkeypatch.setattr(gc, "runtime_config",
                        lambda: {"token": "t", "account_id": "acc-1"})
    monkeypatch.setattr(rpt, "_api_snapshot",
                        lambda token, acc: ([{"sym": "300433.SZ", "qty": 100,
                                               "vwap": 38.91, "fpnl": 12.0,
                                               "last": 39.03}],
                                            {"nav": 100007, "available": 96104,
                                             "market_value": 3903}))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.pkl").write_text(json.dumps(
        {"positions": {"300433.SZ": {"remaining_qty": 100, "stop": 33.94,
                                      "tp1_price": 55.4, "tp2_price": 51.3}}},
        ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rpt, "_name_map", lambda syms: {"300433.SZ": "蓝思科技"})
    from datetime import datetime
    txt = rpt.build_report(datetime(2026, 8, 28, 15, 45))
    assert "掘金日终播报 · 2026-08-28" in txt
    assert "信号 0 → 挂单 0" in txt          # 无 audit 文件=零漏斗
    assert "300433.SZ 蓝思科技 ×100｜成本 38.91｜现 39.03｜浮盈 +12" in txt
    assert "止损 33.94｜止盈 TP1 55.40/TP2 51.30" in txt
    assert "nav 100,007" in txt
