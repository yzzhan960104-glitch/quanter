# -*- coding: utf-8 -*-
"""板块/因子只读端点（server/api/v1/macro.py）契约测试。

设计意图（为什么要有这套测试）：
    这两个端点是前端驾驶舱「板块轮动 + 微观定权」的【唯一后端供给】：
      - /macro/sector/flow：板块资金流排名 + 活跃股池（板块轮动监控）
      - /macro/factors/{symbol}：单标的 ATR 波动率（微观定权）

    原宏观 CTA 端点 /macro/regime 与 /macro/credit（CreditRegime 信贷状态机）
    已于 2026-07 随 CreditRegime 整体下线删除；本测试随之移除对应用例，
    仅保留板块/因子端点的离线降级契约。

    本测试锁死三条契约：
      1) /macro/sector/flow 在无 sector 湖时返回 {sectors:[], pool:[]} 空结构不抛；
      2) /macro/sector/flow 活跃股池走内存湖 reader.symbols()（零 IO，不重读 daily）；
      3) /macro/factors/{symbol} 在无 minute 湖（时序空）时返回 {atr: None} 不抛。

    Why TestClient 复用 server.main:app 单例：DataLakeReader 单例 monkeypatch
    重置 _instance 与 _lakes，模拟「湖未载入」的离线场景以验证降级契约。
"""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """构造 FastAPI TestClient（复用 server.main:app 单例）。

    Why 用 import 后再构造：server.main 模块级 app 已注册全部路由，TestClient
    生命周期内即可触发 lifespan（含 StrategyLoader.scan、多湖 load），CI 无数据湖
    时 lifespan 内的 reader.load 对缺失 parquet 仅记 warning 不阻断（离线降级契约）。
    """
    from server.main import app
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


# --------------------------------------------------------------
# 契约 2：/macro/factors/{symbol} 无时序 → 返 {atr: None} 不抛
# --------------------------------------------------------------

def test_macro_factors_empty_when_no_timeseries(client, monkeypatch):
    """无 minute 湖/时序空 → /macro/factors/{symbol} 返 {atr: None} 不抛。

    离线降级红线：标的时序缺失时返 None 让前端显示「无数据」，
    而非因 ATR 计算异常导致整页崩溃。
    """
    from data.lake_reader import DataLakeReader
    monkeypatch.setattr(DataLakeReader, "_instance", None)
    reader = DataLakeReader.get_instance()
    monkeypatch.setattr(reader, "_lakes", {})

    resp = client.get("/api/v1/macro/factors/000001.SZ")
    assert resp.status_code == 200
    assert resp.json() == {"atr": None}


# --------------------------------------------------------------
# 契约 3：/macro/factors/{symbol} 时序 bar 数 < ATR 窗口(14) → NaN 降级为 None
# --------------------------------------------------------------

def test_factors_atr_none_when_nan(client, monkeypatch):
    """分钟湖标的 bar 数 < ATR 窗口(14) → atr NaN → 端点须返 None（非非法 JSON nan）。

    Why 必须降级：atr() 基于 rolling(14).mean，bar 数不足 14 时末值为 NaN；
    float(NaN) 经 FastAPI 默认编码器会发出字面 "NaN" token，这是非法 JSON，
    前端 JSON.parse/axios 会抛 SyntaxError 致整页白屏——直接违背降级红线。
    故端点必须 pd.isna 守卫把 NaN 转成 None（合法 JSON null）。
    """
    import pandas as pd
    from data.lake_reader import DataLakeReader
    # 注入一个短序列（<14 bar）的 minute 湖：atr 末值必为 NaN
    short_ts = pd.DataFrame(
        {"open": [1] * 5, "high": [2] * 5, "low": [0] * 5, "close": [1] * 5, "volume": [10] * 5},
        index=pd.date_range("2024-01-02", periods=5, freq="min"),
    )
    fake_reader = type("R", (), {"get_timeseries": lambda self, *a, **k: short_ts})()
    monkeypatch.setattr(DataLakeReader, "get_instance", lambda: fake_reader)

    resp = client.get("/api/v1/macro/factors/000001.SZ")
    assert resp.status_code == 200
    # ★ NaN 降级为 None（合法 JSON null），绝非非法 "nan" token
    assert resp.json() == {"atr": None}
