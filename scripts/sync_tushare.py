# -*- coding: utf-8 -*-
"""通用 Tushare 数据集同步 CLI：python scripts/sync_tushare.py <key> [--years N] [--limit N]

设计意图（极简 CLI）：
- key 必须是 config.TUSHARE_DATASETS 已注册的数据集（argparse choices 反射注册表，
  新增数据集零改 CLI）；同步逻辑全部委托 data.tushare_sync.sync_dataset。
- --years：回溯年数（默认 10，与 LAKE_CONFIG.years_default 对齐）。
- --limit：by=symbol 时仅前 N 只标的（冒烟/调试用，避免全市场拉取耗配额）。

用法示例：
  python scripts/sync_tushare.py fina_income              # 全市场利润表（近10年）
  python scripts/sync_tushare.py fina_income --limit 5    # 仅前5只（冒烟）
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.tushare_sync import sync_dataset
from config import TUSHARE_DATASETS


def main():
    ap = argparse.ArgumentParser(description="通用 Tushare 数据集同步")
    ap.add_argument("key", choices=list(TUSHARE_DATASETS.keys()), help="数据集 key")
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None, help="by=symbol 时仅前 N 只标的")
    args = ap.parse_args()
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * args.years)).strftime("%Y-%m-%d")
    symbols = None
    if args.limit:
        from data.tushare_sync import _load_universe
        symbols = _load_universe()[:args.limit]
    sync_dataset(args.key, start, end, symbols=symbols)
    print(f"{args.key} 同步完成")


if __name__ == "__main__":
    main()
