# -*- coding: utf-8 -*-
"""P3 敏感性端点契约测试（2026-08-13 · spec §4.2，直调端点函数，不起 HTTP server）。

物理意图：三个只读端点的返回结构与降级语义契约——敏感性表（边际+排名+死参+盲区）、
热力图（网格+n_obs 同行+fill 旗标）、参数空间（候选档+约束提示）。不起真实 server
（避开 server_lifecycle 标记），用 tmp DB 经函数参数注入。
"""
from __future__ import annotations

import sqlite3

import pytest

from presentation.server.api.v1 import discovery as disc


def _mk_db(tmp_path, rows):
    """建最小 discovery DB（snapshot + trial 两表够端点用）。"""
    db = str(tmp_path / "d.db")
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE snapshot (snapshot_hash TEXT, created_at TEXT);
        CREATE TABLE trial (params TEXT, inner_metrics TEXT, snapshot_hash TEXT);
    """)
    con.execute("INSERT INTO snapshot VALUES ('snap1', '2026-08-13T00:00:00')")
    for params, calmar in rows:
        con.execute("INSERT INTO trial VALUES (?, ?, 'snap1')",
                    (params, '{"calmar": %s, "n": 10}' % calmar))
    con.commit()
    return db


def test_sensitivity_structure_and_dead_param(tmp_path):
    """敏感性端点：结构齐全 + 合成恒值参数（min_rr 三档同 calmar）落 dead_params
    （spec §4.4 锚——借 min_rr 历史死参设定构造零方差场景；P4 后 min_rr 为活参，
     真实语料中不保证被标记）。"""
    import json
    rows = []
    for w, c in [(40, 1.0), (60, 2.0), (80, 3.0)]:
        for rr in [1.0, 1.5, 2.0]:
            rows.append((json.dumps({"window": w, "min_rr": rr, "tp_h_mult": 2.0}), c))
    db = _mk_db(tmp_path, rows)
    out = _call(disc.sensitivity, db)
    assert out["n_trials"] == 9
    assert set(out.keys()) >= {"marginals", "ranking", "dead_params", "blind_spots"}
    # 主效应排名：window 唯一有效应 → 第一
    assert out["ranking"][0]["key"] == "window"
    assert out["dead_params"] == ["min_rr"]


def test_heatmap_grid_with_nobs(tmp_path):
    """热力图端点：grid 均值 + n_obs 同行 + fill 旗标透传。"""
    import json
    rows = [
        (json.dumps({"window": 40, "tp_h_mult": 1.5}), 1.0),
        (json.dumps({"window": 40, "tp_h_mult": 1.5}), 2.0),
        (json.dumps({"window": 60, "tp_h_mult": 2.0}), 3.0),
    ]
    db = _mk_db(tmp_path, rows)
    out = _call(lambda: disc.heatmap(x="window", y="tp_h_mult"), db)
    assert out["x_axis"] == ["40", "60"]
    assert out["grid"][0] == [1.5, None]
    assert out["n_obs"][0] == [2, 0]
    assert out["fill"] is False


def test_params_structure(tmp_path):
    """参数空间端点：候选档三件套 + 约束提示（前端维度选择器联动契约）。"""
    db = _mk_db(tmp_path, [])
    out = _call(disc.params, db)
    assert len(out["param_space"]) == 21
    first = out["param_space"][0]
    assert set(first.keys()) == {"key", "layer", "candidates"}
    assert len(out["constraints"]) == 4


def test_empty_db_degrades(tmp_path):
    """空/缺失 DB → 空结构不抛（离线降级契约，沿 macro 路由惯例）。"""
    db = str(tmp_path / "missing.db")   # 不存在 → 降级
    out = _call(disc.sensitivity, db)
    assert out["n_trials"] == 0
    assert out["ranking"] == []


def test_readonly_router_accessible_without_token(tmp_path):
    """P3-I2 回归（2026-08-13 外部评审）：只读挂载红线自动化——TestClient 全栈起
    FastAPI 挂 discovery_router（**不挂** require_write），无 token GET 必须 200。

    旧测试函数直调绕过依赖注入，「不挂 require_write」仅靠 main.py 注释人工守护。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    _mk_db(tmp_path, [("{}", 1.0)])
    import presentation.server.api.v1.discovery as mod
    old = mod._DISCOVERY_DB
    mod._DISCOVERY_DB = str(tmp_path / "d.db")
    try:
        app = FastAPI()
        app.include_router(mod.router, prefix="/api/v1")   # 无 dependencies=[...] 参数
        client = TestClient(app)
        r = client.get("/api/v1/research/discovery/sensitivity")
        assert r.status_code == 200
        assert r.json()["n_trials"] == 1
    finally:
        mod._DISCOVERY_DB = old


def _call(fn, db):
    """以 tmp DB 调用端点函数（monkeypatch 模块 DB 路径）。"""
    import presentation.server.api.v1.discovery as mod
    old = mod._DISCOVERY_DB
    mod._DISCOVERY_DB = db
    try:
        return fn()
    finally:
        mod._DISCOVERY_DB = old
