# -*- coding: utf-8 -*-
"""交易流水分页查询单测（Task 1 · SSoT Phase A · A4 改造）。

A4 改造（CSV 契约 → DB 契约）：
    原测试锁定 CSV 读口的分页/过滤/大小写/小写规范化契约（显式设
    LIVE_TRADE_READ_SOURCE=csv 走 CSV 回退读口）。A2/A4 删了 CSV 回退读口 +
    LIVE_TRADE_COLUMNS，旧测试用 LIVE_TRADE_LOG monkeypatch + _write_csv 已失效。
    本文件锁定**同一组契约**（分页/方向过滤/标的过滤/小写规范化/空诚实返），
    数据源平移到 state_store.fill 表（A0 tmp_db fixture 构造真相源）。

    物理意图不变：query_trades 是消费端（前端 TradesPage）读成交流水的唯一读口，
    契约红线（direction 必须小写、过滤大小写不敏感、空不抛）必须保持 —— 只是
    真相源从 CSV 镜像改为 fill 表（spec §2.4 SSoT）。
"""
import pytest

from presentation.server.services import trading_service
from trading import state_store


def _insert_fills(db, rows):
    """构造 fill 表真相源（替代 _write_csv · A4 改 DB）。

    rows: [{traded_time, symbol, direction, shares, price, strategy}]
    traded_time 口径：YYYYMMDDHHMMSS 数字串（与 insert_fill 契约一致）。
    """
    for i, r in enumerate(rows):
        state_store.insert_fill(
            f"O{i}", "ACC_TEST", r["traded_time"], r["symbol"],
            r["direction"], r["shares"], r["price"],
            strategy=r.get("strategy"), db_path=db,
        )


def test_query_trades_pagination_and_filter(tmp_db):
    """分页 + 日期/标的/方向过滤（DB 读口契约，A4 平移 from CSV）。

    覆盖：全量 / direction 过滤 / symbol 过滤 / limit-offset 分页。
    """
    _insert_fills(tmp_db, [
        {"traded_time": "20260721093500", "symbol": "510300.SH", "direction": "BUY",
         "shares": 100, "price": 4.0, "strategy": "neckline"},
        {"traded_time": "20260721100000", "symbol": "159915.SZ", "direction": "SELL",
         "shares": 100, "price": 5.0, "strategy": "neckline"},
        {"traded_time": "20260720140000", "symbol": "510300.SH", "direction": "BUY",
         "shares": 200, "price": 3.9, "strategy": "neckline"},
    ])

    # 全量（该日）
    r = trading_service.query_trades("2026-07-21", "2026-07-21")
    assert r["total"] == 2
    assert r["trades"][0]["symbol"] in ("510300.SH", "159915.SZ")

    # 方向过滤
    r = trading_service.query_trades("2026-07-21", "2026-07-21", direction="buy")
    assert r["total"] == 1 and r["trades"][0]["symbol"] == "510300.SH"

    # 标的过滤
    r = trading_service.query_trades("2026-07-20", "2026-07-21", symbol="510300.SH")
    assert r["total"] == 2

    # 分页
    r = trading_service.query_trades("2026-07-20", "2026-07-21", limit=1, offset=0)
    assert r["total"] == 3 and len(r["trades"]) == 1
    assert r["limit"] == 1 and r["offset"] == 0


def test_query_trades_empty_log(tmp_db):
    """DB 空 → trades 空、total=0（诚实空，不抛；DB 读口契约，A4 平移 from CSV）。"""
    r = trading_service.query_trades("2026-07-21", "2026-07-21")
    assert r["total"] == 0 and r["trades"] == []


def test_query_trades_direction_case_insensitive(tmp_db):
    """direction 过滤大小写不敏感（DB 读口契约，A4 平移 from CSV）。

    生产 fill 表存大写 BUY/SELL（与 insert_fill 入参一致），前端/调用方传小写 "buy"
    亦应命中，避免 direction 过滤恒空（query_fills 已 .upper() 归一）。
    """
    _insert_fills(tmp_db, [
        {"traded_time": "20260721093500", "symbol": "510300.SH", "direction": "BUY",
         "shares": 100, "price": 4.0, "strategy": "neckline"},
        {"traded_time": "20260721100000", "symbol": "159915.SZ", "direction": "SELL",
         "shares": 100, "price": 5.0, "strategy": "neckline"},
    ])

    # 小写 "buy" 过滤 → 命中大写 BUY 那行（证明大小写不敏感）
    r = trading_service.query_trades("2026-07-21", "2026-07-21", direction="buy")
    assert r["total"] == 1 and r["trades"][0]["symbol"] == "510300.SH"

    # 大写 "SELL" 过滤 → 同样命中（双向兼容）
    r = trading_service.query_trades("2026-07-21", "2026-07-21", direction="SELL")
    assert r["total"] == 1 and r["trades"][0]["symbol"] == "159915.SZ"


def test_query_trades_direction_normalized_to_lowercase(tmp_db):
    """返回的 direction 必须规范化为小写（final review I1 · 交易 UI 红线，DB 读口）。

    生产 fill 表存大写 BUY/SELL，但前端 TradesTable.vue 用 `row.direction === 'buy'`
    小写精确匹配做方向徽章着色（buy=danger 红 / sell=success 绿）。若 query_trades
    原样透传大写，BUY 行会被前端误判为「非 buy」→ 错挂 success（绿·卖色），SELL 行
    才挂 danger（红·买色）—— 视觉警示与交易动作完全颠倒，是交易 UI 红线 bug。

    本测试断言：即便 DB 存的是大写，query_trades 返回的 trades[].direction 也必须
    是小写，保证服务端是方向口径的单一真相源（消费者一律拿小写）。
    """
    _insert_fills(tmp_db, [
        {"traded_time": "20260721093500", "symbol": "510300.SH", "direction": "BUY",
         "shares": 100, "price": 4.0, "strategy": "neckline"},
        {"traded_time": "20260721100000", "symbol": "159915.SZ", "direction": "SELL",
         "shares": 100, "price": 5.0, "strategy": "neckline"},
    ])

    r = trading_service.query_trades("2026-07-21", "2026-07-21")
    assert r["total"] == 2
    # 服务端必须把大写 BUY/SELL 规范化为小写，前端徽章着色口径才能对齐。
    directions = {t["direction"] for t in r["trades"]}
    assert directions == {"buy", "sell"}, f"direction 未规范化为小写: {directions}"
