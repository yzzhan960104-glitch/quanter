# -*- coding: utf-8 -*-
"""W3.2 消费端切 state_store.fill —— query_trades 优先读 DB 真相源（TDD RED）。

08-04 事故根因：消费端（query_trades/简报/导出）读 CSV 镜像，但 CSV 在重放/补推
场景下会出现重复行（同一笔 (order_id, traded_time) 被 record_live_trade 多次
append），消费端无去重 → 简报把 24 行重复当成「买 24 笔」误导研究员。

T6（commit 7162f385）已让写入端幂等：state_store.fill 是成交流水真相源
（insert_fill 首次成功才写，CSV 同步只在首次写）。本测试锁定消费端切换：
- query_trades 优先读 state_store.fill，DB 有数据不碰 CSV；
- LIVE_TRADE_READ_SOURCE=csv 回退原 CSV 读口（一键回滚开关，spec §5）。

返 shape {trades, total, limit, offset} 不变（前端 TradesPage 契约红线）。
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """每个用例独立 SQLite + 独立 CSV 路径，互不污染。"""
    db = str(tmp_path / "ts.db")
    csv_path = str(tmp_path / "live_trades.csv")
    # state_store 模块级 _DEFAULT_DB 是写入默认值；消费端 query_fills 也读它。
    monkeypatch.setattr("trading.state_store._DEFAULT_DB", db)
    monkeypatch.setattr(
        "presentation.server.services.trading_service.LIVE_TRADE_LOG", csv_path)
    # 缺省 db（W3.2 切换后真相源是 DB）
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "db")
    from trading import state_store
    state_store.init_store(db)
    state_store.upsert_account("acct1", broker="qmt")
    return db, csv_path


def test_query_trades_reads_db_fill_first(isolated_db):
    """W3.2: query_trades 优先读 state_store.fill；DB 有数据时不碰 CSV。"""
    db, csv_path = isolated_db
    from trading import state_store
    # 写一笔真相到 fill 表（首次成功）
    state_store.insert_fill(
        "oid1", "acct1", "20260805101000", "300001.SZ", "BUY", 100, 10.5)
    # 故意不写 CSV —— DB 有数据时 query_trades 不应碰 CSV（CSV 不存在仍能返出）

    # 模块在 fixture 之后 import，确保 monkeypatch 已生效
    from presentation.server.services.trading_service import query_trades
    res = query_trades("2026-08-05", "2026-08-05")
    assert res["total"] == 1
    assert len(res["trades"]) == 1
    row = res["trades"][0]
    assert row["symbol"] == "300001.SZ"
    # direction 规范化为小写口径（与原 query_trades 一致，前端依赖小写精确匹配着色）
    assert row["direction"] == "buy"
    assert row["shares"] == 100.0
    assert row["price"] == 10.5
    # shape 契约（前端 TradesPage 红线）
    for k in ("trades", "total", "limit", "offset"):
        assert k in res


def test_query_trades_db_shape_limit_offset_unchanged(isolated_db):
    """W3.2: 切 DB 后 shape {trades, total, limit, offset} + 分页语义不变。"""
    db, _ = isolated_db
    from trading import state_store
    # 写 3 笔同日成交
    for i in range(3):
        state_store.insert_fill(
            f"oid{i}", "acct1", f"2026080510100{i}", "300001.SZ", "BUY", 100, 10.5 + i)
    from presentation.server.services.trading_service import query_trades
    # limit=2 offset=0 → 命中全集 total=3，page 只 2 行（分页切片语义保持）
    res = query_trades("2026-08-05", "2026-08-05", limit=2, offset=0)
    assert res["total"] == 3
    assert len(res["trades"]) == 2
    assert res["limit"] == 2
    assert res["offset"] == 0


def test_query_trades_fallback_csv_when_env_set(isolated_db, monkeypatch):
    """W3.2: LIVE_TRADE_READ_SOURCE=csv → 回退 CSV 读口（一键回滚开关）。

    场景：DB 空、CSV 有 1 行、env=csv → query_trades 应读 CSV。
    Why 这是回滚保险：DB 异常或运维主动回退时，消费端能从 CSV 镜像继续读。
    """
    db, csv_path = isolated_db
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "csv")
    # 写一行 CSV（真相镜像）
    import csv as _csv
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = _csv.DictWriter(f, fieldnames=[
            "timestamp", "symbol", "direction", "shares", "price",
            "strategy", "rationale", "kind"])
        w.writeheader()
        w.writerow({"timestamp": "2026-08-05 10:10:00", "symbol": "510300.SH",
                    "direction": "BUY", "shares": 200, "price": 4.0,
                    "strategy": "neckline", "rationale": "", "kind": "fill"})
    from presentation.server.services.trading_service import query_trades
    res = query_trades("2026-08-05", "2026-08-05")
    assert res["total"] == 1
    assert res["trades"][0]["symbol"] == "510300.SH"


def test_query_trades_db_empty_no_csv_returns_empty(isolated_db):
    """W3.2: DB 空且无 CSV → 诚实空结果（不抛 FileNotFoundError）。"""
    from presentation.server.services.trading_service import query_trades
    res = query_trades("2026-08-05", "2026-08-05")
    assert res["total"] == 0
    assert res["trades"] == []


def test_query_trades_db_direction_filter_case_insensitive(isolated_db):
    """W3.2: 切 DB 后 direction 过滤大小写不敏感保持（前端传 buy/CSV/DB 大写均命中）。"""
    db, _ = isolated_db
    from trading import state_store
    state_store.insert_fill(
        "oid_b", "acct1", "20260805101000", "300001.SZ", "BUY", 100, 10.5)
    state_store.insert_fill(
        "oid_s", "acct1", "20260805101100", "300001.SZ", "SELL", 100, 10.8)
    from presentation.server.services.trading_service import query_trades
    # 前端传小写 buy（与原 CSV 读口一致），DB 存大写 BUY
    res = query_trades("2026-08-05", "2026-08-05", direction="buy")
    assert res["total"] == 1
    assert res["trades"][0]["direction"] == "buy"


def test_query_trades_db_dedup_same_order_id_traded_time(isolated_db):
    """W3.2: DB fill 表 UNIQUE(order_id, traded_time) 已天然去重 —— 同笔成交重放
    insert_fill 第二次返 False 不落表，消费端看到的始终是 1 笔（修复 08-04 事故根因）。
    """
    db, _ = isolated_db
    from trading import state_store
    # 首次写入成功
    ok1 = state_store.insert_fill(
        "oid_x", "acct1", "20260805101000", "300001.SZ", "BUY", 100, 10.5)
    # 重放（同 order_id + 同 traded_time）→ insert_fill 幂等返 False，DB 不重复记
    ok2 = state_store.insert_fill(
        "oid_x", "acct1", "20260805101000", "300001.SZ", "BUY", 100, 10.5)
    assert ok1 is True
    assert ok2 is False
    from presentation.server.services.trading_service import query_trades
    res = query_trades("2026-08-05", "2026-08-05")
    # 消费端看到的「买 1 笔」而不是「买 2 笔」（08-04 事故根因修复）
    assert res["total"] == 1
