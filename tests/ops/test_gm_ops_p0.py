# -*- coding: utf-8 -*-
"""QMT 退役 P0 三件套：掘金看护 / audit 采集器 / 晨检。

方案源：docs/superpowers/plans/2026-08-27-qmt-decommission-gm-sole-source.md
（G-1/G-2/G-3）。测试焦点：窗口判定、探测降级语义、采集幂等与 account 继承、
晨检账实漂移判定——外部 I/O（socket/API/进程）全部 monkeypatch 隔离。
"""
from __future__ import annotations

import json
from datetime import datetime

from ops import emquant_audit_ingest as ingest
from ops import emquant_morning_check as mc
from ops import gm_terminal_guard as guard


# ============================================================ G-1 看护
def test_market_window_boundaries():
    assert guard._in_market_window(datetime(2026, 8, 27, 10, 0))       # 周四盘中
    assert not guard._in_market_window(datetime(2026, 8, 27, 8, 0))    # 盘前过早
    assert not guard._in_market_window(datetime(2026, 8, 27, 16, 0))   # EOD 后
    assert not guard._in_market_window(datetime(2026, 8, 29, 10, 0))   # 周六


def test_guard_healthy_and_degraded(monkeypatch):
    monkeypatch.setattr(guard, "_in_market_window", lambda: True)     # 钉盘中（防窗外时刻跑测试）
    monkeypatch.setattr(guard, "probe_port_7001", lambda: True)
    monkeypatch.setattr(guard, "probe_api", lambda cfg: (True, 200))
    monkeypatch.setattr(guard, "probe_strategy_process", lambda d: 1)
    monkeypatch.setattr(guard.gc, "runtime_config",
                        lambda: {"token": "t", "account_id": "a"})
    st = guard.run_once(auto_heal=False)
    assert st["ok"] and st["problems"] == []

    monkeypatch.setattr(guard, "probe_port_7001", lambda: False)       # 终端挂
    monkeypatch.setattr(guard, "probe_strategy_process", lambda d: 0)
    st = guard.run_once(auto_heal=False)
    assert not st["ok"] and any("7001" in p for p in st["problems"])
    assert any("策略进程" in p for p in st["problems"])                # 盘中缺进程


def test_guard_auto_heal_only_when_terminal_healthy(monkeypatch):
    """自愈前置=终端健康：终端挂时绝不瞎拉策略（拉起也连不上，徒增噪声）。

    钉单腿（gc.active_legs→main）：.env 部署 GM_EXP_STRATEGY_DIR 后真实环境是
    双腿（heal 计数=腿数），本用例断言的是「终端健康与否」这一单变量，隔离腿数。"""
    monkeypatch.setattr(guard.gc, "active_legs",
                        lambda: [guard.gc.LEGS[0]])
    monkeypatch.setattr(guard, "_in_market_window", lambda: True)
    monkeypatch.setattr(guard, "probe_api", lambda cfg: (True, 200))
    monkeypatch.setattr(guard.gc, "runtime_config",
                        lambda: {"token": "t", "account_id": "a"})
    healed = {"n": 0}

    def fake_heal(d):
        healed["n"] += 1
        return True

    monkeypatch.setattr(guard, "auto_heal_strategy", fake_heal)
    monkeypatch.setattr(guard, "probe_strategy_process", lambda d: 0)
    monkeypatch.setattr(guard, "probe_port_7001", lambda: True)
    st = guard.run_once(auto_heal=True)
    assert st["auto_healed"] and healed["n"] == 1 and st["ok"]

    monkeypatch.setattr(guard, "probe_port_7001", lambda: False)       # 终端死
    st = guard.run_once(auto_heal=True)
    assert not st["auto_healed"] and healed["n"] == 1                   # 不再拉


# ============================================================ G-2 采集器
def _write_audit(path, rows):
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        for r in rows:
            w.writerow(r)


def test_ingest_idempotent_and_account_inheritance(tmp_path):
    day = "2026-08-27"
    audit = tmp_path / "audit" / "audit_20260827.csv"
    _write_audit(audit, [
        [f"{day}T09:31:00", "INIT",
         json.dumps({"account": "acc-1", "strategy_id": "st-1", "build_stamp": "x"})],
        [f"{day}T13:31:00", "ORDER_PLACED", json.dumps({"symbol": "300433.SZ", "qty": 100})],
    ])
    r1 = ingest.ingest_day(day, db_path=tmp_path / "t.db", strategy_dir=tmp_path)
    assert r1["inserted"] == 2
    r2 = ingest.ingest_day(day, db_path=tmp_path / "t.db", strategy_dir=tmp_path)
    assert r2["inserted"] == 0 and r2["skipped"] == 2          # 幂等：重跑零重复

    import sqlite3
    with sqlite3.connect(tmp_path / "t.db") as con:
        rows = con.execute(
            "SELECT event, account_id, strategy_id FROM terminal_audit ORDER BY ts").fetchall()
    assert rows[0] == ("INIT", "acc-1", "st-1")
    assert rows[1] == ("ORDER_PLACED", "acc-1", "st-1")        # 非 INIT 行继承当日锚点

    r3 = ingest.ingest_day("2026-08-26", db_path=tmp_path / "t.db",
                           strategy_dir=tmp_path)
    assert r3["file"] is None and r3["inserted"] == 0           # 缺文件=无操作不报错


# ============================================================ G-3 晨检
def test_morning_check_drift_detection(tmp_path, monkeypatch):
    day = f"{datetime.now():%Y-%m-%d}"
    _write_audit(tmp_path / "audit" / f"audit_{day.replace('-', '')}.csv",
                 [[f"{day}T09:31:00", "INIT",
                   json.dumps({"account": "acc-1", "strategy_id": "st-1",
                               "build_stamp": "s"})]])
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.pkl").write_text(json.dumps({
        "last_pre_open_date": day,
        "positions": {"300433.SZ": {"remaining_qty": 100},
                      "600000.SH": {"remaining_qty": 0}}}), encoding="utf-8")

    monkeypatch.setattr(mc.gc, "GM_STRATEGY_DIR", tmp_path)
    monkeypatch.setattr(guard, "probe_port_7001", lambda: True)
    monkeypatch.setattr(guard, "probe_api", lambda cfg: (True, 200))
    monkeypatch.setattr(guard, "probe_strategy_process", lambda d: 1)
    monkeypatch.setattr(mc, "probe_port_7001", lambda: True)
    monkeypatch.setattr(mc, "probe_api", lambda cfg: (True, 200))
    monkeypatch.setattr(mc, "probe_strategy_process", lambda d: 1)

    monkeypatch.setattr(mc.gc, "runtime_config",
                        lambda d=None: {"token": "t", "account_id": "acc-1"})
    monkeypatch.setattr(mc, "_api_position_symbols",
                        lambda token, acc: ["300433.SZ"])
    checks = {c["name"]: c for c in mc.run_checks()}
    assert checks["终端网关"]["ok"] and checks["策略进程"]["ok"]
    assert checks["INIT账户核"]["ok"] and checks["盘前已跑"]["ok"]
    assert checks["账实对账"]["ok"]                      # API=state={300433}

    monkeypatch.setattr(mc, "_api_position_symbols",
                        lambda token, acc: ["300433.SZ", "301018.SZ"])
    checks = {c["name"]: c for c in mc.run_checks()}
    assert not checks["账实对账"]["ok"] and "漂移" in checks["账实对账"]["detail"]
