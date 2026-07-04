"""龙虎榜日频同步：AKShare → data_lake/dragon_list.parquet（MultiIndex(date, symbol)）。

龙虎榜反映游资/机构活跃个股，上榜频次是关注度/情绪因子。逐日拉
stock_lhb_detail_daily_sina → 合并 MultiIndex(date, symbol)，供
factors/alternative.dragon_signal 取当日上榜集合。

用法：python scripts/sync_dragon_list.py --days 30
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

# 加项目根到 sys.path（脚本可从任意 cwd 直接跑）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from tqdm import tqdm

from config import LAKE_CONFIG
from data.clients.akshare_client import AKShareClient


def _normalize_symbol(code: str) -> str:
    """6 位代码 → 带 .SH/.SZ 后缀（与 daily 湖 symbol 一致）。6/9 开头上交所。"""
    c = str(code).strip().zfill(6)
    return f"{c}.SH" if c.startswith(("6", "9")) else f"{c}.SZ"


def sync_dragon_list(days: int, out: str) -> None:
    client = AKShareClient()
    today = datetime.today()
    date_list = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    pieces = []
    for d in tqdm(date_list):
        df = client.fetch_dragon_list(d)
        if df is None or df.empty:
            continue
        # 代码列模糊匹配（akshare 列名含「代码」/「symbol」）
        code_col = next((c for c in df.columns
                         if "代码" in str(c) or "symbol" in str(c).lower()), None)
        if code_col is None:
            continue
        sub = pd.DataFrame({
            "date": pd.to_datetime(d),
            "symbol": df[code_col].astype(str).map(_normalize_symbol),
            "hit": 1,  # 上榜标记；同时确保 parquet 有数据列（无列 MultiIndex parquet 读回会丢层级名）
        })
        pieces.append(sub)

    if not pieces:
        print("龙虎榜全空（akshare 限频/网络/无上榜日），跳过。请稍后重试。")
        return

    big = pd.concat(pieces, ignore_index=True)
    big = big.drop_duplicates(subset=["date", "symbol"]).set_index(["date", "symbol"]).sort_index()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    big.to_parquet(out, engine="pyarrow")
    print(f"龙虎榜湖写入：{out}，{len(big)} 行，"
          f"{big.index.get_level_values('date').nunique()} 个交易日")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="龙虎榜日频数据湖同步（AKShare）")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default=LAKE_CONFIG["lakes"]["dragon_list"])
    args = ap.parse_args()
    sync_dragon_list(days=args.days, out=args.out)
