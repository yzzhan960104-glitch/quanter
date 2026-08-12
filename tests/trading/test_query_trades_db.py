# -*- coding: utf-8 -*-
"""W3.2 消费端切 state_store.fill —— query_trades 读 DB 真相源（TDD RED）。

08-04 事故根因：消费端（query_trades/简报/导出）读 CSV 镜像，但 CSV 在重放/补推
场景下会出现重复行（同一笔 (order_id, traded_time) 被原 record_live_trade 多次
append），消费端无去重 → 简报把 24 行重复当成「买 24 笔」误导研究员。

T6（commit 7162f385）已让写入端幂等：state_store.fill 是成交流水真相源
（insert_fill 首次成功才写）。本测试锁定消费端切换：
- query_trades 读 state_store.fill（DB 是唯一真相源，spec §2.4 SSoT）。

A4 收口（CSV 回退已退役）：
- 原 LIVE_TRADE_READ_SOURCE=csv 回退开关（W3.2 spec §5 一键回滚路径）在 A2 已删，
  对应 3 个 *_fallback_csv_when_env_set 测试随之退役（无回退路径可测）。
- 原 isolated_db fixture 的 LIVE_TRADE_LOG monkeypatch 删除（常量 A4 删）。

返 shape {trades, total, limit, offset} 不变（前端 TradesPage 契约红线）。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """每个用例独立 SQLite，互不污染（A4 删 CSV 路径，仅返 db）。"""
    db = str(tmp_path / "ts.db")
    # state_store 模块级 _DEFAULT_DB 是写入默认值；消费端 query_fills 也读它。
    monkeypatch.setattr("trading.state_store._DEFAULT_DB", db)
    from trading import state_store
    state_store.init_store(db)
    state_store.upsert_account("acct1", broker="qmt")
    return db


def test_query_trades_reads_db_fill_first(isolated_db):
    """W3.2: query_trades 优先读 state_store.fill；DB 有数据时不碰 CSV。"""
    db = isolated_db
    from trading import state_store
    # 写一笔真相到 fill 表（首次成功）
    state_store.insert_fill(
        "oid1", "acct1", "20260805101000", "300001.SZ", "BUY", 100, 10.5)
    # 故意不写 CSV —— DB 有数据时 query_trades 不应碰 CSV（CSV 不存在仍能返出）

    # 模块在 fixture 之后 import，确保 monkeypatch 已生效
    from trading.gateway_service import query_trades
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
    db = isolated_db
    from trading import state_store
    # 写 3 笔同日成交
    for i in range(3):
        state_store.insert_fill(
            f"oid{i}", "acct1", f"2026080510100{i}", "300001.SZ", "BUY", 100, 10.5 + i)
    from trading.gateway_service import query_trades
    # limit=2 offset=0 → 命中全集 total=3，page 只 2 行（分页切片语义保持）
    res = query_trades("2026-08-05", "2026-08-05", limit=2, offset=0)
    assert res["total"] == 3
    assert len(res["trades"]) == 2
    assert res["limit"] == 2
    assert res["offset"] == 0


def test_query_trades_db_empty_no_csv_returns_empty(isolated_db):
    """W3.2: DB 空且无 CSV → 诚实空结果（不抛 FileNotFoundError）。"""
    from trading.gateway_service import query_trades
    res = query_trades("2026-08-05", "2026-08-05")
    assert res["total"] == 0
    assert res["trades"] == []


def test_query_trades_db_direction_filter_case_insensitive(isolated_db):
    """W3.2: 切 DB 后 direction 过滤大小写不敏感保持（前端传 buy/CSV/DB 大写均命中）。"""
    db = isolated_db
    from trading import state_store
    state_store.insert_fill(
        "oid_b", "acct1", "20260805101000", "300001.SZ", "BUY", 100, 10.5)
    state_store.insert_fill(
        "oid_s", "acct1", "20260805101100", "300001.SZ", "SELL", 100, 10.8)
    from trading.gateway_service import query_trades
    # 前端传小写 buy（与原 CSV 读口一致），DB 存大写 BUY
    res = query_trades("2026-08-05", "2026-08-05", direction="buy")
    assert res["total"] == 1
    assert res["trades"][0]["direction"] == "buy"


def test_query_trades_db_dedup_same_order_id_traded_time(isolated_db):
    """W3.2: DB fill 表 UNIQUE(order_id, traded_time) 已天然去重 —— 同笔成交重放
    insert_fill 第二次返 False 不落表，消费端看到的始终是 1 笔（修复 08-04 事故根因）。
    """
    db = isolated_db
    from trading import state_store
    # 首次写入成功
    ok1 = state_store.insert_fill(
        "oid_x", "acct1", "20260805101000", "300001.SZ", "BUY", 100, 10.5)
    # 重放（同 order_id + 同 traded_time）→ insert_fill 幂等返 False，DB 不重复记
    ok2 = state_store.insert_fill(
        "oid_x", "acct1", "20260805101000", "300001.SZ", "BUY", 100, 10.5)
    assert ok1 is True
    assert ok2 is False
    from trading.gateway_service import query_trades
    res = query_trades("2026-08-05", "2026-08-05")
    # 消费端看到的「买 1 笔」而不是「买 2 笔」（08-04 事故根因修复）
    assert res["total"] == 1


# ============================================================================
# W3.2 完整收口（用户两轴 review）：export_trades 切 query_fills（spec §3.3.2）
# ============================================================================
def test_export_trades_reads_db_fill_first(isolated_db):
    """W3.2 收口：export_trades 优先读 state_store.fill，DB 有数据不碰 CSV。

    物理意图（spec §3.3.2）：「导出接口保留 CSV 导出，但数据源改为从 DB 生成」。
    原 export_trades 流式读 CSV，在重放场景下会导出 24 行重复，污染 Layer6 复盘输入。
    切 DB 后，fill 表 UNIQUE(order_id, traded_time) 天然去重，导出永远是真相源。
    """
    db = isolated_db
    from trading import state_store
    state_store.insert_fill(
        "oid_e1", "acct1", "20260805101000", "300001.SZ", "BUY", 100, 10.5)
    # 故意不写 CSV —— DB 有数据时 export_trades 不应碰 CSV

    from trading.gateway_service import export_trades, _EXPORT_COLUMNS
    csv_text = export_trades("2026-08-05", "2026-08-05")
    # 表头契约（前端下载依赖 _EXPORT_COLUMNS 表头顺序 · A4：LIVE_TRADE_COLUMNS 已删）
    header_line = csv_text.splitlines()[0]
    assert header_line == ",".join(_EXPORT_COLUMNS)
    # 数据行：1 行 fill（DB 真相源）
    data_lines = [ln for ln in csv_text.splitlines()[1:] if ln.strip()]
    assert len(data_lines) == 1, f"DB 1 笔成交应只导出 1 行，实际 {len(data_lines)}"
    # symbol / direction（DB 存大写，CSV 导出落大写口径与原 CSV 读口一致）
    assert "300001.SZ" in data_lines[0]
    assert "BUY" in data_lines[0]


def test_export_trades_empty_db_returns_header_only(isolated_db):
    """W3.2 收口：DB 空 + 无 CSV → 只返表头（诚实空导出，前端照常下载，非 404）。

    注：DB 走 csv.DictWriter（默认 \\r\\n 行尾），CSV 回退走手拼 "\\n" —— 两路径
    行尾不一致但表头行内容一致，前端 Excel/csv-parse 均兼容。本测试只断言表头内容。
    """
    from trading.gateway_service import export_trades, _EXPORT_COLUMNS
    csv_text = export_trades("2026-08-05", "2026-08-05")
    # 表头行内容 = _EXPORT_COLUMNS（A4：LIVE_TRADE_COLUMNS 已删，_EXPORT_COLUMNS 是唯一源）
    first_line = csv_text.splitlines()[0]
    assert first_line == ",".join(_EXPORT_COLUMNS)
    # 无数据行
    assert len([ln for ln in csv_text.splitlines()[1:] if ln.strip()]) == 0


# ============================================================================
# W3.4 完整收口（用户两轴 review）：aggregate_fills_by_symbol 切 fill 表（spec §3.3.4）
# ============================================================================
def test_aggregate_fills_reads_db_not_csv_on_replay(isolated_db, monkeypatch):
    """W3.4 收口：重放场景（CSV 24 行重复）→ aggregate 读 fill 表只 1 笔净持仓。

    物理意图（spec §3.3.4 + 08-04 事故还原）：
        post_close 归因聚合原流式读 CSV，24 行重复 BUY 100 → 净 2400 股幻影，
        归因日志误报 drift。切 fill 表后（UNIQUE 去重），同笔成交只 1 行 → 净 100，
        与 position_book 对齐，归因日志正确。
    """
    db = isolated_db
    from trading import state_store
    # fill 表只 1 笔真相（insert_fill 幂等，重放返 False）
    state_store.insert_fill(
        "oid_a1", "acct1", "20260805101000", "600000.SH", "BUY", 100, 10.0)
    state_store.insert_fill(
        "oid_a1", "acct1", "20260805101000", "600000.SH", "BUY", 100, 10.0)  # 重放返 False

    # A4 注：原测试同时写 24 行重复 CSV 证明 aggregate 不读 CSV（只读 DB 1 笔真相）。
    # CSV 写盘链路已整体退役（record_live_trade 删），CSV 重复源不再存在；保留 fill
    # 表 1 笔真相的断言锁定 aggregate 读 fill 的契约。

    from trading.gateway_service import aggregate_fills_by_symbol
    net = aggregate_fills_by_symbol("2026-08-05", "2026-08-05")
    # 切 fill 表后只 1 笔真相 → 净 100（不是 CSV 24 行污染的 2400）
    assert net.get("600000.SH") == 100.0, (
        f"aggregate 读 fill 表应只 1 笔净 100，实际 {net.get('600000.SH')}"
        "（若读 CSV 24 行重复会得 2400）")


def test_aggregate_fills_buy_sell_netting_db(isolated_db):
    """W3.4 收口：BUY/SELL 净聚合（fill 表）—— 买 100 + 卖 60 → 净 +40。"""
    db = isolated_db
    from trading import state_store
    state_store.insert_fill(
        "oid_b", "acct1", "20260805101000", "300001.SZ", "BUY", 100, 10.0)
    state_store.insert_fill(
        "oid_s", "acct1", "20260805101100", "300001.SZ", "SELL", 60, 10.5)
    from trading.gateway_service import aggregate_fills_by_symbol
    net = aggregate_fills_by_symbol("2026-08-05", "2026-08-05")
    assert net.get("300001.SZ") == 40.0  # 100 - 60


