# -*- coding: utf-8 -*-
"""risk_control 人工风控双值单测（ADR-16 · 2026-08-17）。

覆盖契约：
1. 空表（缺键）→ resolve 默认 block=False / max_pos=1.0（零行为变化起步）；
2. 写读往返：block on/off、position 数值 → resolve 反映新值（UPSERT 幂等可覆盖）；
3. 值域校验 fail-closed：position 越界 / 空写 → ValueError 拒写（不静默钳制）；
4. DB 异常 → resolve fail-closed 全拦（block=True / max_pos=0.0 / degraded=True）；
5. raw 读坏值（直写非法字符串）→ resolve 按默认 + degraded=True（观测可辨）；
6. CLI 冒烟：block on / position 0.8 / 查看（独立进程语义，单进程内直调 _main）。
"""
from __future__ import annotations

import sqlite3

import pytest

from trading import state_store


@pytest.fixture
def risk_db(tmp_path, monkeypatch):
    """隔离的 trading_state.db（risk_control 表随 init_store 建）。"""
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    state_store.init_store()
    return db


def test_empty_table_resolves_defaults(risk_db):
    """空表缺键 → block=False / max_pos=1.0 / degraded=False（ADR-16 零行为变化起步）。"""
    r = state_store.resolve_risk_control()
    assert r == {"block": False, "max_pos": 1.0, "degraded": False}


def test_write_read_roundtrip_block(risk_db):
    """block on → 拦；再 off → 放行（UPSERT 覆盖）。"""
    r1 = state_store.write_risk_control(block_new_orders=True)
    assert r1["block"] is True and r1["degraded"] is False
    assert state_store.resolve_risk_control()["block"] is True
    r2 = state_store.write_risk_control(block_new_orders=False)
    assert r2["block"] is False
    raw = state_store.read_risk_control()
    assert raw["block_new_orders"] == "0"


def test_write_read_roundtrip_position(risk_db):
    """position 0.8 → resolve 0.8；raw 存数值字符串。"""
    state_store.write_risk_control(max_total_position=0.8)
    r = state_store.resolve_risk_control()
    assert r["max_pos"] == pytest.approx(0.8)
    assert state_store.read_risk_control()["max_total_position"] == "0.8000"


def test_write_position_out_of_range_rejected(risk_db):
    """position 越界（1.5 / -0.1）→ ValueError 拒写（人工风控意志设错必须显式失败）。"""
    with pytest.raises(ValueError, match="越界"):
        state_store.write_risk_control(max_total_position=1.5)
    with pytest.raises(ValueError, match="越界"):
        state_store.write_risk_control(max_total_position=-0.1)
    # 拒写后表仍空（缺键默认）
    assert state_store.resolve_risk_control()["max_pos"] == 1.0


def test_write_no_keys_rejected(risk_db):
    """两键全 None → 空写拒收。"""
    with pytest.raises(ValueError, match="至少传一个键"):
        state_store.write_risk_control()


def test_resolve_fail_closed_on_db_error(tmp_path):
    """DB 打不开（路径是目录等）→ fail-closed 全拦 + degraded。"""
    bad = str(tmp_path / "no_such_dir" / "x" / "state.db")
    r = state_store.resolve_risk_control(db_path=bad)
    assert r == {"block": True, "max_pos": 0.0, "degraded": True}


def test_resolve_degrades_on_corrupt_value(risk_db):
    """raw 坏值（绕过 write 直写非法字符串）→ 按默认执行 + degraded=True。"""
    with sqlite3.connect(risk_db) as con:
        con.execute("INSERT INTO risk_control(key, value, updated_at) VALUES(?, ?, ?)",
                    ("block_new_orders", "yes", "2026-08-17T00:00:00"))
        con.execute("INSERT INTO risk_control(key, value, updated_at) VALUES(?, ?, ?)",
                    ("max_total_position", "abc", "2026-08-17T00:00:00"))
    r = state_store.resolve_risk_control()
    assert r["block"] is False and r["max_pos"] == 1.0 and r["degraded"] is True


def test_cli_block_and_position(risk_db, capsys, monkeypatch):
    """CLI 冒烟：block on → position 0.8 → 查看输出（直调 _main，同独立进程语义）。"""
    from trading import risk_ctrl
    monkeypatch.setattr("sys.argv", ["risk_ctrl"])
    assert risk_ctrl._main(["block", "on"]) == 0
    assert state_store.resolve_risk_control()["block"] is True
    assert risk_ctrl._main(["position", "0.8"]) == 0
    r = state_store.resolve_risk_control()
    assert r["block"] is True and r["max_pos"] == pytest.approx(0.8)
    assert risk_ctrl._main([]) == 0
    out = capsys.readouterr().out
    assert "拦截增量下单" in out and "80%" in out


def test_cli_rejects_bad_args(risk_db, capsys):
    """CLI 非法参数 → 退出码 2 + stderr 提示（不落库）。"""
    from trading import risk_ctrl
    assert risk_ctrl._main(["block", "maybe"]) == 2
    assert risk_ctrl._main(["position", "1.5"]) == 2
    assert state_store.resolve_risk_control()["block"] is False
