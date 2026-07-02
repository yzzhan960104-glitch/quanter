# -*- coding: utf-8 -*-
"""Task 16：宏观/板块/因子只读端点（server/api/v1/macro.py）契约测试。

设计意图（为什么要有这套测试）：
    这四个端点是宏观 CTA 前端驾驶舱（T17 /dashboard）的【唯一后端供给】：
      - /macro/regime：当前宏观信贷状态 + 近 N 日历史迁移（红/黄/绿宏观灯）
      - /macro/credit：社融/M1M2_gap/dr007 时序曲线（信贷三因子趋势）
      - /macro/sector/flow：板块资金流排名 + 活跃股池（板块轮动监控）
      - /macro/factors/{symbol}：单标的 ATR 波动率（微观定权）

    本测试锁死五条契约：
      1) /macro/regime 返回的 regime ∈ {+1, 0, -1}（CreditRegime 三态契约）；
      2) /macro/regime 返回 {regime, history} 双字段结构（前端拆灯+曲线）；
      3) /macro/credit 在无 macro 湖时返回空结构 {"series": {}} 而非抛异常
         （离线降级红线：开发机/CI 无数据湖时前端容错）；
      4) /macro/sector/flow 在无 sector 湖时返回 {sectors:[], pool:[]} 空结构不抛；
      5) /macro/factors/{symbol} 在无 minute 湖（时序空）时返回 {atr: None} 不抛。

    Why monkeypatch CreditRegime.get_default：单例生产侧由 lifespan 注入 macro 湖，
    单测隔离即替换 get_default 返合成 _FakeRegime，绕开真实 parquet IO，保证测试
    纯净可复现。DataLakeReader 单例同样 monkeypatch 重置 _instance 与 _lakes，
    模拟「湖未载入」的离线场景以验证降级契约。
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
# 辅助：合成 CreditRegime 替身（绕开真实 macro 湖 IO）
# --------------------------------------------------------------

class _FakeRegime:
    """CreditRegime 的测试替身。

    Why 自替身而非注入 macro_df：get_default() 单例在生产侧由 lifespan 装配宏观湖，
    单测路径用 monkeypatch.setattr 直接替换类方法返回 _FakeRegime，最快隔离。
    compute/history 返回值结构与端点契约严格对齐（history 返 [{date, regime}] 列表，
    与 /macro/regime 端点对 history 字段的期望一致）。
    """

    def compute(self, date) -> int:
        return 1

    def history(self, n: int = 60) -> list[dict]:
        return [{"date": "2024-01-02", "regime": 1}]


# --------------------------------------------------------------
# 契约 1：/macro/regime 返回 {regime, history} 且 regime ∈ {+1,0,-1}
# --------------------------------------------------------------

def test_macro_regime_endpoint(client, monkeypatch):
    """/macro/regime 返当前 regime 与近 N 日 history，regime ∈ {+1,0,-1}。

    物理意图：前端驾驶舱红/黄/绿宏观灯 + 历史迁移带的数据源。
    regime=+1（扩张，绿灯）/ 0（中性，黄灯）/ -1（收缩，红灯）三态严格枚举，
    history 为近 60 日逐日状态序列（前端绘红黄绿带）。
    """
    from factors.macro_regime import CreditRegime
    # 重置单例避免其他测试污染本断言（_FakeRegime 完全绕开真实 macro 湖）
    monkeypatch.setattr(CreditRegime, "get_default", lambda: _FakeRegime())

    resp = client.get("/api/v1/macro/regime")
    assert resp.status_code == 200
    body = resp.json()
    # 三态契约：扩张/中性/收缩；任何其它值都是 CreditRegime 判别逻辑漂移
    assert body["regime"] in (-1, 0, 1)
    # 双字段结构：regime（当前态）+ history（历史迁移曲线）
    assert "history" in body


# --------------------------------------------------------------
# 契约 2：/macro/credit 无 macro 湖 → 返空 {series: {}} 不抛
# --------------------------------------------------------------

def test_macro_credit_empty_when_no_lake(client, monkeypatch):
    """无 macro 湖（离线/CI 无 parquet）→ /macro/credit 返 {series:{}} 不抛异常。

    离线降级红线：开发机/CI 无数据湖时，端点必须返空结构而非 500，
    让前端能渲染空图表容错；任何抛异常都会让前端整页白屏。
    """
    # 重置 DataLakeReader 单例 + 清空 _lakes 模拟「湖未载入」
    from data.lake_reader import DataLakeReader
    monkeypatch.setattr(DataLakeReader, "_instance", None)
    reader = DataLakeReader.get_instance()
    monkeypatch.setattr(reader, "_lakes", {})

    resp = client.get("/api/v1/macro/credit")
    assert resp.status_code == 200
    assert resp.json() == {"series": {}}


# --------------------------------------------------------------
# 契约 3：/macro/credit 有 macro 湖 → 返时序字典
# --------------------------------------------------------------

def test_macro_credit_with_data(client, monkeypatch):
    """macro 湖已载入 → /macro/credit 返 {列名: [{date,value}]} 时序结构。

    物理意图：前端绘社融/M1M2_gap/dr007 三条信贷因子趋势曲线。
    """
    from data.lake_reader import DataLakeReader
    monkeypatch.setattr(DataLakeReader, "_instance", None)
    reader = DataLakeReader.get_instance()
    # 注入合成宏观湖（DatetimeIndex，无 symbol 层，匹配 macro 湖落盘结构）
    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    fake_macro = pd.DataFrame(
        {"shrzgm": [100.0, 101.0, 102.0, 103.0, 104.0]}, index=idx
    )
    monkeypatch.setattr(reader, "_lakes", {"macro": fake_macro})

    resp = client.get("/api/v1/macro/credit")
    assert resp.status_code == 200
    body = resp.json()
    assert "series" in body
    assert "shrzgm" in body["series"]
    assert len(body["series"]["shrzgm"]) == 5
    # 每条记录为 {date, value} 双字段结构（前端 v-chart 标准格式）
    assert body["series"]["shrzgm"][0] == {"date": "2024-01-01", "value": 100.0}


# --------------------------------------------------------------
# 契约 4：/macro/sector/flow 无 sector 湖 → 返空 {sectors:[], pool:[]} 不抛
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


# --------------------------------------------------------------
# 契约 5：/macro/factors/{symbol} 无时序 → 返 {atr: None} 不抛
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
# 契约 6：/macro/factors/{symbol} 时序 bar 数 < ATR 窗口(14) → NaN 降级为 None
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
