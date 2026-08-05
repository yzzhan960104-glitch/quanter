# -*- coding: utf-8 -*-
"""一次性可信回填：把历史 live_trades.csv 的 kind=fill 真实成交补入 state_store（SSoT）。

背景（2026-08-05 决策）：消费端（简报/导出/复盘）已切 state_store.fill，历史真实成交
（补录/成交回报）只存在于 CSV，历史日期导出/复盘会缺失。本脚本做一次性可信回填，
排除测试/冒烟行（300001.SZ/300002.SZ/600000.SH 的「成交回报@」、510300 冒烟、DRYRUN.SZ）
——判定复用 scripts/migrate_live_trades_csv._is_test_row，与历史清理口径单一。

幂等：insert_fill UNIQUE(order_id, traded_time)；order_id 由 CSV 行合成
（CSV_BACKFILL_<timestamp 数字>_<symbol>），重复执行跳过，不重复累加持仓。

默认 dry-run 只统计不写库；--apply 才落库（含 account/position 累加）。

用法：
    .venv310/Scripts/python.exe scripts/backfill_live_trades_to_state_store.py --apply
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.archive.migrate_live_trades_csv import _is_test_row  # noqa: E402 历史清理口径复用（A4 归档：两脚本同迁 archive/）
from trading import state_store  # noqa: E402


def iter_trusted_fills(csv_path: str):
    """yield 可回填的 fill 行：kind=fill、方向/数量/价格合法、非测试/冒烟行。"""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("kind") or "").lower() != "fill":
                continue
            sym = r.get("symbol", "")
            direction = (r.get("direction") or "").upper()
            shares, price = r.get("shares"), r.get("price")
            if not sym or direction not in ("BUY", "SELL") or not shares or not price:
                continue
            row7 = [
                r.get("timestamp", ""), sym, direction, shares, price,
                r.get("strategy", ""), r.get("rationale", ""),
            ]
            if _is_test_row(row7):
                continue
            yield r


def backfill(csv_path: str, *, apply: bool = False, db_path: str | None = None,
             account_id: str | None = None) -> dict:
    """回填 trusted fills 到 state_store；apply=False 只统计。返 {candidates, applied, skipped}。"""
    fills = list(iter_trusted_fills(csv_path))
    if not apply:
        return {"candidates": len(fills), "applied": 0, "skipped": 0}
    state_store.init_store(db_path)
    if account_id is None:
        account_id = os.getenv("QMT_ACCOUNT_ID", "default")
    if state_store.get_account(account_id, db_path=db_path) is None:
        state_store.upsert_account(account_id, broker="qmt", db_path=db_path)
    applied = skipped = 0
    for r in fills:
        ts = r["timestamp"]
        # fill 表 traded_time 契约：YYYYMMDDHHMMSS 数字串（query_fills 按 substr(...,1,8) 过滤）
        traded_time = "".join(ch for ch in ts if ch.isdigit())
        sym = r["symbol"]
        direction = (r["direction"] or "").upper()
        order_id = f"CSV_BACKFILL_{traded_time}_{sym}"
        inserted = state_store.insert_fill(
            order_id, account_id, traded_time, sym, direction,
            float(r["shares"]), float(r["price"]), db_path=db_path)
        if inserted:
            applied += 1
            state_store.apply_fill_to_position(
                account_id, sym, direction,
                float(r["shares"]), float(r["price"]), traded_time, db_path=db_path)
        else:
            skipped += 1
    if applied:
        # 建仓日锁定为最早成交日：apply_fill_to_position 无 entry_date 入参，会用
        # clock.today()（回填日）——对历史持仓会把 holding_days 算成 0，必须按最早
        # CSV_BACKFILL 成交日回填（YYYYMMDD，与 traded_time 前缀同口径）。
        import sqlite3 as _sqlite3
        con = _sqlite3.connect(db_path or state_store._DEFAULT_DB)
        try:
            con.execute(
                "UPDATE position SET entry_date = ("
                " SELECT MIN(substr(fill.traded_time, 1, 8)) FROM fill"
                " WHERE fill.symbol = position.symbol AND fill.direction = 'BUY'"
                " AND fill.order_id LIKE 'CSV_BACKFILL_%'"
                ") WHERE account_id = ? AND entry_date = ?",
                (account_id, state_store.clock.today()))
            con.commit()
        finally:
            con.close()
    return {"candidates": len(fills), "applied": applied, "skipped": skipped}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="历史 CSV 成交可信回填 state_store（默认 dry-run）")
    # 默认源 CSV 指向归档目录（A4 归档：logs/live_trades.csv 已 mv 到 logs/archive/，
    # 归档后此脚本仍可重跑，--csv 显式覆盖亦兼容老路径）
    p.add_argument("--csv", default="logs/archive/live_trades.csv.final-20260805",
                   help="源 CSV（默认 logs/archive/live_trades.csv.final-20260805）")
    p.add_argument("--apply", action="store_true", help="真正落库（默认只统计）")
    p.add_argument("--db", default=None, help="state_store DB 路径（默认 logs/trading_state.db）")
    p.add_argument("--account", default=None, help="account_id（默认 QMT_ACCOUNT_ID / default）")
    args = p.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except ImportError:
        pass
    res = backfill(args.csv, apply=args.apply, db_path=args.db, account_id=args.account)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
