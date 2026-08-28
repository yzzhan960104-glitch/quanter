# -*- coding: utf-8 -*-
"""gm 只读代理双腿形态（2026-08-29 cockpit 多腿方案 §2）。

测试焦点：腿目录聚合（LEGS 单源+策略名映射+未部署降级）、?leg= 参数化（缺省 main
向后兼容+非法腿 404）、/ab 的 compare 单源消费（single_leg 降级）、/audit 的按腿
过滤与只读连接。7002 调用与 runtime 读取全部 monkeypatch 隔离——零外网/零终端依赖。
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ops import emquant_ab_compare as ab
from ops import gm_ops_common as gc
from presentation.server.api.v1 import gm as gm_api


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """最小 app：只挂 gm 路由（不拉起 main.py 全量 lifespan）。"""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(gm_api.router, prefix="/api/v1")
    return TestClient(app)


# ─────────────────────────── 腿目录 ───────────────────────────
def test_legs_aggregates_registry_and_strategy_names(client, monkeypatch):
    monkeypatch.setattr(gc, "active_legs", lambda: list(gc.LEGS))
    monkeypatch.setattr(gc, "leg_strategy_dir", lambda leg: {
        gc.LEGS[0]: _rt_dir("main"), gc.LEGS[1]: _rt_dir("exp")}[leg])
    monkeypatch.setattr(gc, "runtime_config", lambda d=None: {
        "main": {"token": "t", "account_id": "acc-main", "strategy_id": "sid-main"},
        "exp": {"token": "t", "account_id": "acc-exp", "strategy_id": "sid-exp"},
    }[_key_of(d)])
    monkeypatch.setattr(gc, "api_get", lambda path, token, timeout=4.0: (200, {
        "data": [{"strategy_id": "sid-main", "name": "NECK"},
                 {"strategy_id": "sid-exp", "name": "NECK-EXP"}]}))
    r = client.get("/api/v1/gm/legs")
    assert r.status_code == 200
    legs = r.json()["legs"]
    assert [l["key"] for l in legs] == ["main", "exp"]
    assert legs[0]["role"] == "incumbent" and legs[1]["role"] == "challenger"
    assert legs[1]["strategy_name"] == "NECK-EXP" and legs[1]["account_id"] == "acc-exp"


_RT = {"main": {"token": "t", "account_id": "acc-main", "strategy_id": "sid-main"},
       "exp": {"token": "t", "account_id": "acc-exp", "strategy_id": "sid-exp"}}


def _key_of(d):
    if d is None or str(d).endswith("main_dir"):
        return "main"
    return "exp"


def _rt_dir(key):
    from pathlib import Path
    return Path(f"C:/fake/{key}_dir")


def _patch_legs(monkeypatch, both=True):
    legs = list(gc.LEGS) if both else [gc.LEGS[0]]
    monkeypatch.setattr(gc, "active_legs", lambda: legs)
    monkeypatch.setattr(gc, "leg_strategy_dir", lambda leg: _rt_dir(leg.key))
    monkeypatch.setattr(gc, "runtime_config", lambda d=None: _RT[_key_of(d)])


# ─────────────────────────── ?leg= 参数化 ───────────────────────────
def test_asset_default_leg_is_main_backward_compatible(client, monkeypatch):
    calls = {}

    def fake_get(path, token, timeout=4.0):
        calls["path"] = path
        return 200, {"data": [{"nav": 1}]}

    _patch_legs(monkeypatch)
    monkeypatch.setattr(gc, "api_get", fake_get)
    r = client.get("/api/v1/gm/asset")
    assert r.status_code == 200 and calls["path"].endswith("acc-main")


def test_asset_exp_leg_routes_to_exp_account(client, monkeypatch):
    calls = {}

    def fake_get(path, token, timeout=4.0):
        calls["path"] = path
        return 200, {"data": [{"nav": 2}]}

    _patch_legs(monkeypatch)
    monkeypatch.setattr(gc, "api_get", fake_get)
    r = client.get("/api/v1/gm/asset?leg=exp")
    assert r.status_code == 200 and calls["path"].endswith("acc-exp")


def test_unknown_leg_404(client, monkeypatch):
    _patch_legs(monkeypatch)
    assert client.get("/api/v1/gm/asset?leg=ghost").status_code == 404


def test_gateway_down_502_with_leg_hint(client, monkeypatch):
    _patch_legs(monkeypatch)
    monkeypatch.setattr(gc, "api_get", lambda path, token, timeout=4.0: (0, None))
    r = client.get("/api/v1/gm/trades?leg=exp")
    assert r.status_code == 502 and "leg=exp" in r.json()["detail"]


# ─────────────────────────── /ab 单源消费 ───────────────────────────
def test_ab_single_leg_degrades_not_errors(client, monkeypatch):
    _patch_legs(monkeypatch, both=False)
    r = client.get("/api/v1/gm/ab")
    assert r.status_code == 200 and r.json()["single_leg"] is True


def test_ab_uses_compare_single_source(client, monkeypatch, tmp_path):
    _patch_legs(monkeypatch)
    monkeypatch.setattr(gm_api, "_AB_DB", tmp_path / "t.db")
    with sqlite3.connect(tmp_path / "t.db") as con:
        con.execute(ab_ingest_ddl())
        for acc, stamp in (("acc-main", "s"), ("acc-exp", "s [exp]")):
            con.execute("INSERT INTO terminal_audit(ts,event,detail,account_id,strategy_id,ingested_at)"
                        " VALUES (?,?,?,?,?,?)",
                        ("2026-08-29T09:15:00", "INIT",
                         json.dumps({"account": acc, "build_stamp": stamp}), acc, "sid", "t"))
    seen = {}

    def fake_compare(day, m, e, ma, ea):
        seen["rows"] = (len(m), len(e), ma, ea)
        return {"day": day, "main": {}, "exp": {}, "signals": {"added": [], "removed": [], "drifted": []},
                "orders": {"added": [], "removed": [], "drifted": [], "qty_diff": []}, "flags": ["x"]}

    monkeypatch.setattr(gm_api.ab, "compare", fake_compare)
    r = client.get("/api/v1/gm/ab?date=2026-08-29")
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False and body["flags"] == ["x"]
    assert seen["rows"] == (1, 1, "acc-main", "acc-exp")      # 行按账户过滤后进 compare


def ab_ingest_ddl():
    from ops.emquant_audit_ingest import _DDL
    return _DDL


# ─────────────────────────── /audit 下钻 ───────────────────────────
def test_audit_filters_by_leg_and_event(client, monkeypatch, tmp_path):
    _patch_legs(monkeypatch)
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as con:
        con.execute(ab_ingest_ddl())
        rows = [
            ("2026-08-29T09:31:08", "SIGNAL", '{"symbol": "300433.SZ"}', "acc-main"),
            ("2026-08-29T09:31:08", "SIGNAL", '{"symbol": "688825.SH"}', "acc-exp"),
            ("2026-08-29T10:31:00", "SCHEDULE_TICK", '{"probe": true}', "acc-main"),
            ("2026-08-28T09:31:08", "SIGNAL", '{"symbol": "old"}', "acc-main"),
        ]
        for ts, ev, d, a in rows:
            con.execute("INSERT INTO terminal_audit(ts,event,detail,account_id,strategy_id,ingested_at)"
                        " VALUES (?,?,?,?,?,?)", (ts, ev, d, a, "sid", "t"))
    monkeypatch.setattr(gm_api, "_AB_DB", db)
    r = client.get("/api/v1/gm/audit?leg=main&date=2026-08-29&event=SIGNAL")
    body = r.json()
    assert r.status_code == 200
    syms = [row["detail"].get("symbol") for row in body["rows"]]
    assert syms == ["300433.SZ"]                    # 腿过滤+日期过滤+事件过滤（exp 行/隔日行均排除）


def test_audit_missing_db_returns_placeholder(client, monkeypatch, tmp_path):
    _patch_legs(monkeypatch)
    monkeypatch.setattr(gm_api, "_AB_DB", tmp_path / "nope.db")
    r = client.get("/api/v1/gm/audit")
    assert r.status_code == 200 and r.json()["rows"] == []
