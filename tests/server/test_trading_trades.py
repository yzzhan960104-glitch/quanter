# -*- coding: utf-8 -*-
"""交易流水分页查询单测（Task 1）。

覆盖 query_trades 契约：
- 分页（limit/offset）+ total 计数
- 日期闭区间（timestamp 日期前缀比较）
- symbol/direction 精确过滤
- CSV 不存在的诚实空降级（不抛）
"""
import csv
import os

from server.services import trading_service


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
    """分页 + 日期/标的/方向过滤。"""
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
    """CSV 不存在 → 空 trades、total=0（诚实空，不抛）。"""
    monkeypatch.setattr(trading_service, "LIVE_TRADE_LOG", str(tmp_path / "nope.csv"))
    r = trading_service.query_trades("2026-07-21", "2026-07-21")
    assert r["total"] == 0 and r["trades"] == []


def test_query_trades_direction_case_insensitive(tmp_path, monkeypatch):
    """direction 过滤大小写不敏感（跨 Task 1 latent 回归）。

    生产落 CSV 的是大写口径（server/services/trading_service.py:432 写 BUY/SELL），
    前端/调用方传小写 "buy" 亦应命中，避免 direction 过滤恒空。
    """
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
