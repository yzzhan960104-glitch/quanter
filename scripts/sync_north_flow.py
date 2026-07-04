"""北向资金日频同步：AKShare → data_lake/north_flow.parquet（DatetimeIndex 单序列湖）。

北向资金（沪深股通净流入）是外资情绪的代理变量：连续大额净流入常领先大盘上涨，
持续流出常领先回调。落盘 DatetimeIndex × [north_net_flow]（亿元），供
factors/alternative.north_flow_momentum 算连续净流入动量信号。

用法：python scripts/sync_north_flow.py --years 2
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

# 加项目根到 sys.path（脚本可从任意 cwd 直接跑）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import LAKE_CONFIG
from data.clients.akshare_client import AKShareClient


def sync_north_flow(years: int, out: str) -> None:
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    df = AKShareClient().fetch_north_flow(start, end)
    if df is None or df.empty:
        print("北向资金为空（akshare 限频/网络），跳过。请稍后重试。")
        return
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_parquet(out)
    print(f"北向资金湖写入：{out}，{len(df)} 行，{df.index.min()}~{df.index.max()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="北向资金日频数据湖同步（AKShare）")
    ap.add_argument("--years", type=int, default=2)
    ap.add_argument("--out", default=LAKE_CONFIG["lakes"]["north_flow"])
    args = ap.parse_args()
    sync_north_flow(years=args.years, out=args.out)
