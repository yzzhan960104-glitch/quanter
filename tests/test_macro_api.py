# -*- coding: utf-8 -*-
"""板块只读端点（server/api/v1/macro.py）契约测试。

设计意图（为什么要有这套测试）：
    /macro/sector/flow 是前端驾驶舱「板块轮动」的【唯一后端供给】：
      - /macro/sector/flow：板块资金流排名 + 活跃股池（板块轮动监控）

    原宏观 CTA 端点 /macro/regime 与 /macro/credit（CreditRegime 信贷状态机）
    已于 2026-07 随 CreditRegime 整体下线删除；/macro/factors/{symbol}（单标的
    ATR 微观定权）于 T7 架构治理批 2 删除（前端零调用方，颈线法自带 compute_atr
    不依赖 factors）。本测试随之移除对应用例，仅保留板块端点的离线降级契约。

    本测试锁死两条契约：
      1) /macro/sector/flow 在无 sector 湖时返回 {sectors:[], pool:[]} 空结构不抛；
      2) /macro/sector/flow 活跃股池走内存湖 reader.symbols()（零 IO，不重读 daily）。

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
# 契约 1：/macro/sector/flow 无 sector 湖 → 返空 {sectors:[], pool:[]} 不抛
# --------------------------------------------------------------

def test_macro_sector_flow_empty_when_no_lake(client, monkeypatch):
    """无 sector/daily parquet → /macro/sector/flow 返 {sectors:[], pool:[]} 不抛。

    离线降级红线：板块资金流缺失时前端容错渲染空表。
    端点直读 parquet（sector 是快照表非时序，不走 DataLakeReader），故 mock 路径不存在。
    """
    from config import LAKE_CONFIG
    monkeypatch.setitem(LAKE_CONFIG["lakes"], "sector", "/nonexistent/sector.parquet")
    monkeypatch.setitem(LAKE_CONFIG["lakes"], "daily", "/nonexistent/daily.parquet")

    resp = client.get("/api/v1/macro/sector/flow")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sectors"] == []
    assert body["pool"] == []


def test_macro_sector_flow_pool_from_reader_symbols(client, monkeypatch):
    """#5：活跃股池走内存湖 reader.symbols()，不重读 408MB daily parquet。

    物理意图：原实现每请求 read_parquet(daily) 仅取 symbol 列表，是 dashboard 性能黑洞；
    改走 reader.symbols()（内存湖 index）零 IO。mock reader.symbols 返固定列表验证接线，
    且 sector parquet 不存在时 sectors=[] 而 pool 来自 reader.symbols（证明不再依赖 daily parquet）。
    """
    from data.lake_reader import DataLakeReader
    from config import LAKE_CONFIG
    fake_reader = type("R", (), {
        "symbols": lambda self, lake=None: ["000001.SZ", "600000.SH", "510300.SH"],
    })()
    monkeypatch.setattr(DataLakeReader, "get_instance", lambda: fake_reader)
    monkeypatch.setitem(LAKE_CONFIG["lakes"], "sector", "/nonexistent/sector.parquet")

    resp = client.get("/api/v1/macro/sector/flow")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sectors"] == []
    assert [p["symbol"] for p in body["pool"]] == ["000001.SZ", "600000.SH", "510300.SH"]

