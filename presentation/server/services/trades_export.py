# -*- coding: utf-8 -*-
"""实盘成交 CSV 导出（W6-A 收编版，2026-08-28 完成退役）。

物理定位：原 trading.gateway_service.export_trades（gateway_service 随 QMT live
面删除）——实现本体只读 state_store.fill 表（保留面），与券商网关零耦合，故随
唯一消费者（review_service 复盘）迁入 server/services。

契约不变（前端下载红线）：
    - 字段顺序 _EXPORT_COLUMNS（timestamp,symbol,direction,shares,price,strategy,
      rationale,kind）与原 LIVE_TRADE_COLUMNS 同值同序；
    - DB 异常 → 仅表头字符串（不抛不回退，SSoT：fill 表是唯一真相源）；
    - 无数据 → 诚实空导出（仅表头）。
"""
from __future__ import annotations

import csv
import io
import logging

from trading import state_store

logger = logging.getLogger(__name__)

_EXPORT_COLUMNS = [
    "timestamp", "symbol", "direction", "shares", "price",
    "strategy", "rationale", "kind",
]


def export_trades(start: str, end: str) -> str:
    """按日期区间 [start, end]（YYYY-MM-DD）导出实盘成交 CSV 字符串·DB-only。"""
    try:
        rows = state_store.query_fills(start, end)
    except Exception:
        logger.exception("query_fills 读 DB 失败，export 返仅表头（不回退 CSV）")
        return ",".join(_EXPORT_COLUMNS) + "\n"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS)
    writer.writeheader()
    for r in rows:
        tt = str(r.get("traded_time") or "")
        ts = (
            f"{tt[0:4]}-{tt[4:6]}-{tt[6:8]} {tt[8:10]}:{tt[10:12]}:{tt[12:14]}"
            if len(tt) >= 14 else tt
        )
        writer.writerow({
            "timestamp": ts,
            "symbol": r.get("symbol", ""),
            "direction": (r.get("direction") or "").upper(),
            "shares": r.get("shares", ""),
            "price": r.get("price", ""),
            "strategy": r.get("strategy") or "",
            "rationale": "",
            "kind": "fill",
        })
    return buf.getvalue()


def aggregate_fills_by_symbol(start: str, end: str) -> dict[str, float]:
    """聚合 [start, end] 内 BUY/SELL 净持仓 · DB-only（自 gateway_service 收编，实现逐字）。

    SSoT 红线：唯一数据源 state_store.fill（UNIQUE 去重，防 08-04 式重复行幻象持仓）；
    DB 异常 → logger.exception + 返 {}（不回退 CSV）。
    """
    try:
        rows = state_store.query_fills(start, end)
    except Exception:
        logger.exception("query_fills 读 DB 失败，aggregate 返空（不回退 CSV）")
        return {}
    net: dict[str, float] = {}
    for r in rows:
        sym = r.get("symbol")
        direction = (r.get("direction") or "").upper()
        shares = r.get("shares")
        if not sym or direction not in ("BUY", "SELL") or shares is None:
            continue
        net[sym] = net.get(sym, 0.0) + (
            float(shares) if direction == "BUY" else -float(shares))
    return net


def query_trades(start: str, end: str, symbol: str | None = None,
                 direction: str | None = None, limit: int = 100,
                 offset: int = 0) -> dict:
    """分页查询实盘成交流水 · DB-only（自 gateway_service 收编，实现逐字）。

    返回 {trades, total, limit, offset}（前端/broadcast 播报契约 shape）；
    DB 异常 → 空结果不抛（SSoT：不回退 CSV）。limit 钳 [1,1000]、offset >= 0。
    """
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    try:
        rows = state_store.query_fills(start, end, symbol=symbol, direction=direction)
    except Exception:
        logger.exception("query_fills 读 DB 失败，query_trades 返空（不回退 CSV）")
        return {"trades": [], "total": 0, "limit": limit, "offset": offset}
    matched: list = []
    for r in rows:
        tt = str(r.get("traded_time") or "")
        ts = (
            f"{tt[0:4]}-{tt[4:6]}-{tt[6:8]} {tt[8:10]}:{tt[10:12]}:{tt[12:14]}"
            if len(tt) >= 14 else tt
        )
        matched.append({
            "timestamp": ts,
            "traded_time": tt,
            "symbol": r.get("symbol", ""),
            "direction": (r.get("direction") or "").lower(),
            "shares": float(r.get("shares") or 0.0),
            "price": float(r.get("price") or 0.0),
            "strategy": r.get("strategy") or "",
            "rationale": "",
            "kind": "fill",
            "order_id": r.get("order_id", ""),
        })
    total = len(matched)
    page = matched[offset: offset + limit]
    return {"trades": page, "total": total, "limit": limit, "offset": offset}
