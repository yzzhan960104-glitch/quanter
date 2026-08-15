# -*- coding: utf-8 -*-
"""活跃股池只读端点（server/api/v1/macro.py）契约测试。

设计意图（为什么要有这套测试）：
    /macro/pool 是前端驾驶舱「活跃股池」的【唯一后端供给】：
      - /macro/pool：活跃股池（daily 内存湖前 50 只）

    下线史（本测试随之收缩，防复活）：
      原宏观 CTA 端点 /macro/regime 与 /macro/credit（CreditRegime 信贷状态机）
      已于 2026-07 随 CreditRegime 整体下线删除；/macro/factors/{symbol}（单标的
      ATR 微观定权）于 T7 架构治理批 2 删除（前端零调用方）；/macro/sector/flow
      （板块资金流 + 活跃股池复合端点）于 2026-08-15 CR-8 处置删除——sector 湖
      2026-07-27 退役后 sectors 字段结构性恒空，端点收缩为纯活跃股池 /macro/pool。

    本测试锁死两条契约：
      1) /macro/pool 在无 daily 湖时返回 {pool: []} 空结构不抛；
      2) /macro/pool 活跃股池走内存湖 reader.symbols()（零 IO，不重读 daily）。

    Why TestClient 复用 presentation.server.main:app 单例：DataLakeReader 单例 monkeypatch
    重置 _instance 与 _lakes，模拟「湖未载入」的离线场景以验证降级契约。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """构造 FastAPI TestClient（复用 presentation.server.main:app 单例）。

    Why 用 import 后再构造：presentation.server.main 模块级 app 已注册全部路由，TestClient
    生命周期内即可触发 lifespan（含 StrategyLoader.scan、多湖 load），CI 无数据湖
    时 lifespan 内的 reader.load 对缺失 parquet 仅记 warning 不阻断（离线降级契约）。
    """
    from presentation.server.main import app
    return TestClient(app)


# --------------------------------------------------------------
# 契约 0：旧路径 /macro/sector/flow 已删（CR-8 · 2026-08-15）——防复活钉
# --------------------------------------------------------------

def test_macro_sector_flow_route_removed(client):
    """/macro/sector/flow 不再注册（CR-8 下线）——请求应 404 而非 200。

    Why 钉死：sector 湖 2026-07-27 退役后该端点 sectors 恒空，属「结构性恒空死端」；
    若有人复活旧路由（复制历史代码），本用例让契约 gate 之外多一层行为层防线。
    """
    resp = client.get("/api/v1/macro/sector/flow")
    assert resp.status_code == 404


# --------------------------------------------------------------
# 契约 1：/macro/pool 无 daily 湖 → 返空 {pool: []} 不抛
# --------------------------------------------------------------

def test_macro_pool_empty_when_no_lake(client, monkeypatch):
    """无 daily parquet → /macro/pool 返 {pool: []} 不抛。

    离线降级红线：daily 湖缺失时前端容错渲染空表（watermark 兜底），端点不得抛 5xx。
    """
    from data.lake_reader import DataLakeReader
    # mock reader.symbols 返空列表，模拟「daily 湖未载入」的离线场景
    fake_reader = type("R", (), {
        "symbols": lambda self, lake=None: [],
    })()
    monkeypatch.setattr(DataLakeReader, "get_instance", lambda: fake_reader)

    resp = client.get("/api/v1/macro/pool")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pool"] == []


def test_macro_pool_from_reader_symbols(client, monkeypatch):
    """#5：活跃股池走内存湖 reader.symbols()，不重读 408MB daily parquet。

    物理意图：原实现每请求 read_parquet(daily) 仅取 symbol 列表，是 dashboard 性能黑洞；
    改走 reader.symbols()（内存湖 index）零 IO。mock reader.symbols 返固定列表验证接线，
    并验证 pool 元素形态为 {symbol: 代码} 记录（前端表格 code 列依赖此形状）。
    """
    from data.lake_reader import DataLakeReader
    fake_reader = type("R", (), {
        "symbols": lambda self, lake=None: ["000001.SZ", "600000.SH", "510300.SH"],
    })()
    monkeypatch.setattr(DataLakeReader, "get_instance", lambda: fake_reader)

    resp = client.get("/api/v1/macro/pool")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["symbol"] for p in body["pool"]] == ["000001.SZ", "600000.SH", "510300.SH"]
