# -*- coding: utf-8 -*-
"""交易流水分页查询单测（Task 1）。

W3.2 起 query_trades 默认读 state_store.fill（真相源），CSV 是降级镜像。本组
测试锁定 CSV 读口的分页/过滤/大小写/小写规范化契约 —— 显式设 LIVE_TRADE_READ_SOURCE=csv
走 CSV 回退读口（仍是 spec §5 支持的一键回滚路径，契约必须保持不变）。
"""
import csv
import os

import pytest

from presentation.server.services import trading_service


def _write_csv(path, rows):
    """写样本 live_trades.csv（覆盖 trading_service.LIVE_TRADE_LOG）。

    utf-8-sig 与生产 record_live_trade 写盘一致（带 BOM，DictReader 可透明读）。
    """
    cols = trading_service.LIVE_TRADE_COLUMNS
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def test_query_trades_pagination_and_filter(tmp_path, monkeypatch):
    """分页 + 日期/标的/方向过滤（CSV 读口契约，W3.2 走 env=csv 回退路径）。"""
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "csv")
    log = tmp_path / "live_trades.csv"
    monkeypatch.setattr(trading_service, "LIVE_TRADE_LOG", str(log))
    _write_csv(str(log), [
        {"timestamp": "2026-07-21 09:35:00", "symbol": "510300.SH", "direction": "buy",
         "shares": 100, "price": 4.0, "strategy": "neckline", "rationale": "test"},
        {"timestamp": "2026-07-21 10:00:00", "symbol": "159915.SZ", "direction": "sell",
         "shares": 100, "price": 5.0, "strategy": "neckline", "rationale": "tp"},
        {"timestamp": "2026-07-20 14:00:00", "symbol": "510300.SH", "direction": "buy",
         "shares": 200, "price": 3.9, "strategy": "neckline", "rationale": "test"},
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


def test_query_trades_empty_log(tmp_path, monkeypatch):
    """CSV 不存在 → 空 trades、total=0（诚实空，不抛；CSV 回退读口）。"""
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "csv")
    monkeypatch.setattr(trading_service, "LIVE_TRADE_LOG", str(tmp_path / "nope.csv"))
    r = trading_service.query_trades("2026-07-21", "2026-07-21")
    assert r["total"] == 0 and r["trades"] == []


def test_query_trades_direction_case_insensitive(tmp_path, monkeypatch):
    """direction 过滤大小写不敏感（CSV 读口契约，W3.2 走 env=csv 回退路径）。

    生产落 CSV 的是大写口径（server/services/trading_service.py:432 写 BUY/SELL），
    前端/调用方传小写 "buy" 亦应命中，避免 direction 过滤恒空。
    """
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "csv")
    log = tmp_path / "live_trades.csv"
    monkeypatch.setattr(trading_service, "LIVE_TRADE_LOG", str(log))
    _write_csv(str(log), [
        {"timestamp": "2026-07-21 09:35:00", "symbol": "510300.SH", "direction": "BUY",
         "shares": 100, "price": 4.0, "strategy": "neckline", "rationale": "test"},
        {"timestamp": "2026-07-21 10:00:00", "symbol": "159915.SZ", "direction": "SELL",
         "shares": 100, "price": 5.0, "strategy": "neckline", "rationale": "tp"},
    ])

    # 小写 "buy" 过滤 → 命中大写 BUY 那行（证明大小写不敏感）
    r = trading_service.query_trades("2026-07-21", "2026-07-21", direction="buy")
    assert r["total"] == 1 and r["trades"][0]["symbol"] == "510300.SH"

    # 大写 "SELL" 过滤 → 同样命中（双向兼容）
    r = trading_service.query_trades("2026-07-21", "2026-07-21", direction="SELL")
    assert r["total"] == 1 and r["trades"][0]["symbol"] == "159915.SZ"


def test_query_trades_direction_normalized_to_lowercase(tmp_path, monkeypatch):
    """返回的 direction 必须规范化为小写（final review I1 · 交易 UI 红线，CSV 读口）。

    生产落 CSV 是大写 BUY/SELL，但前端 TradesTable.vue 用 `row.direction === 'buy'`
    小写精确匹配做方向徽章着色（buy=danger 红 / sell=success 绿）。若 query_trades
    原样透传大写，BUY 行会被前端误判为「非 buy」→ 错挂 success（绿·卖色），
    SELL 行才挂 danger（红·买色）—— 视觉警示与交易动作完全颠倒，是交易 UI 红线 bug。

    本测试断言：即便 CSV 写盘是大写，query_trades 返回的 trades[].direction 也必须是小写，
    保证服务端是方向口径的单一真相源（消费者一律拿小写）。
    """
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "csv")
    log = tmp_path / "live_trades.csv"
    monkeypatch.setattr(trading_service, "LIVE_TRADE_LOG", str(log))
    _write_csv(str(log), [
        {"timestamp": "2026-07-21 09:35:00", "symbol": "510300.SH", "direction": "BUY",
         "shares": 100, "price": 4.0, "strategy": "neckline", "rationale": "test"},
        {"timestamp": "2026-07-21 10:00:00", "symbol": "159915.SZ", "direction": "SELL",
         "shares": 100, "price": 5.0, "strategy": "neckline", "rationale": "tp"},
    ])

    r = trading_service.query_trades("2026-07-21", "2026-07-21")
    assert r["total"] == 2
    # 服务端必须把大写 BUY/SELL 规范化为小写，前端徽章着色口径才能对齐。
    directions = {t["direction"] for t in r["trades"]}
    assert directions == {"buy", "sell"}, f"direction 未规范化为小写: {directions}"
