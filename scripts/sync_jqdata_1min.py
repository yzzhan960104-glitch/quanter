"""JQData 分钟同步：对活跃池(50只)拉近 3 月 1m/5m，断点续传，配额耗尽优雅停。

Why（分钟层数据湖的核心约束）：
    试用期 100 万条/天 + 单连接，分钟数据量大（50 只 × 90 天 × 240 根/日 × 1m/5m
    汇总极易触百万级）；shard 落盘可断点续传（已拉的下次直接跳过，省配额），
    QuotaExceeded 即停（明日重跑从断点继续），不崩、不越界扣费/封号。

数据流：
    活跃池(Task 6) → 逐只 fetch_minute_bars(start, end, freq) → 落
    data_lake/jq_shards/{symbol}_{freq}.parquet（断点续传 shard）→
    全量成功后合并为 MultiIndex(date,symbol) → data_lake/a_shares_1min.parquet。
"""
from __future__ import annotations

import datetime as _dt
import os
import sys

# 加项目根到 sys.path：脚本可从任意 cwd 直接 `python scripts/xxx.py` 运行。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from tqdm import tqdm

from data.clients.jqdata_client import JQDataClient, QuotaExceeded


def build_multiindex(shard_dir: str, out: str) -> None:
    """合并 shards → MultiIndex(date,symbol) → parquet。

    Why MultiIndex(date,symbol)：下游因子计算按 symbol groupby、按 date 对齐宏观锚点，
    双层索引让 .groupby(level='symbol') / .loc[idx[:,sym],:] 直接可用，省去每次 reset_index。

    shard 文件名约定 `{symbol}_{freq}.parquet`（如 000001.SZ_5m.parquet）。
    解析 symbol：用 rsplit('_', 1)[0] 取最后一个 _ 之前的全部字符作 symbol（容忍
    symbol 内含 _，freq 后缀恒定在末尾）。date 列：_cleanse 已把 index 设为
    DatetimeIndex，reset_index 后命名为 date。
    """
    frames: list[pd.DataFrame] = []
    for f in os.listdir(shard_dir):
        if not f.endswith(".parquet"):
            continue
        sym = f.rsplit("_", 1)[0]  # 文件名去掉 _freq.parquet 后缀 = symbol
        df = pd.read_parquet(os.path.join(shard_dir, f))
        df["symbol"] = sym
        # reset_index 把 DatetimeIndex 提升为 date 列（_cleanse 已洗净为 tz-naive）
        df = df.reset_index().rename(columns={"index": "date"})
        # 兼容兜底：若 index 本就有名字（如 'datetime'），reset_index 后列名非 'date'
        if "date" not in df.columns:
            df = df.rename(columns={df.columns[0]: "date"})
        frames.append(df)
    if not frames:
        # shard 空（活跃池全停牌/全空）→ 不崩，显式提示上游
        raise RuntimeError(f"shard 空：{shard_dir}")
    big = pd.concat(frames, ignore_index=True)
    big["date"] = pd.to_datetime(big["date"])
    big = big.set_index(["date", "symbol"]).sort_index()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    big.to_parquet(out)
    print(f"分钟湖写入：{out}，{len(big)} 行")


def sync_jqdata_1min(
    pool: list[str],
    months: int = 3,
    freq: str = "5m",
    shard_dir: str = "data_lake/jq_shards",
    out: str = "data_lake/a_shares_1min.parquet",
    start: str | None = None,
    end: str | None = None,
) -> None:
    """循环活跃池拉分钟 K：断点续传（已存在跳过）+ 优雅停（QuotaExceeded 即停）。

    Why 断点续传：聚宽按条计费 + 日 100 万配额，分钟数据量大，首日很可能拉不完。
    每标的独立 shard，重跑时 os.path.exists 即跳过 → 已拉的不再消耗配额，从断点续。

    Why 优雅停：QuotaExceeded 代表【已触日配额红线】，再发任何请求都可能越界扣费/封号。
    故捕获后立即 break，绝不拉后续标的，打印"明日重跑续传"。不向上抛（让调度器崩
    整个 sync 链路无意义——配额耗尽是预期的运维场景）。

    参数：
        pool:      活跃股 symbol 列表（Task 6 select_active_pool 输出，如 '000001.SZ'）。
        months:    回看月数（默认 3，×30 天近似）。
        freq:      '1m' 或 '5m'（默认 5m，与 config JQDATA_CONFIG.freq_default 一致）。
        shard_dir: 断点续传 shard 目录（data_lake/jq_shards）。
        out:       合并输出 parquet（data_lake/a_shares_1min.parquet）。
    """
    os.makedirs(shard_dir, exist_ok=True)
    # 日期范围：start/end 优先（试用账号须落在权限窗口内，如 2025-03-24~2026-03-31）；
    # 缺省回看 months 个月（×30 天近似）。
    end = end or _dt.date.today().strftime("%Y-%m-%d")
    start = start or (_dt.date.today() - _dt.timedelta(days=30 * months)).strftime("%Y-%m-%d")
    client = JQDataClient.get_instance()
    stopped = False
    for sym in tqdm(pool, desc=f"JQData {freq}"):
        shard = os.path.join(shard_dir, f"{sym}_{freq}.parquet")
        # 断点续传：已存在即跳过，省配额（重跑场景下首日已拉的不再重拉）
        if os.path.exists(shard):
            continue
        try:
            df = client.fetch_minute_bars(sym, start, end, frequency=freq)
        except QuotaExceeded:
            # 优雅停：配额耗尽即停后续标的，绝不越界扣费/封号；明日重跑从断点续传
            print("今日额度将尽，明日重跑续传")
            stopped = True
            break
        if df.empty:
            # 个股空结果（停牌/新股）→ 跳过落盘，不写空 shard（避免下次被误判"已拉"）
            continue
        df.to_parquet(shard)
    if not stopped:
        # 全量成功才合并；优雅停时不合并（shard 不全，合并无意义，等明日续传完毕再合）
        try:
            build_multiindex(shard_dir, out)
        except RuntimeError as e:
            # shard 全空（活跃池空/全停牌）→ 仅打印，不崩
            print(e)


# 兜底池：活跃池空（融资融券未拉到 / 非交易日）时用这批流动性高的沪深300核心股，保证分钟湖可建。
_FALLBACK_POOL = [
    "000001.SZ", "600519.SH", "000858.SZ", "601318.SH", "600036.SH",
    "000333.SZ", "601166.SH", "002594.SZ", "600276.SH", "601888.SH",
]


if __name__ == "__main__":
    import argparse
    from config import AKSHARE_CONFIG, JQDATA_CONFIG
    from data.clients.akshare_client import AKShareClient
    from scripts.sync_sector_daily import select_active_pool

    ap = argparse.ArgumentParser(description="JQData 分钟级数据湖同步")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD（试用账号须落在权限窗口内，如 2025-03-24）")
    ap.add_argument("--end", default=None, help="结束日 YYYY-MM-DD（缺省=今日）")
    ap.add_argument("--freq", default=JQDATA_CONFIG["freq_default"], choices=["1m", "5m"])
    ap.add_argument("--months", type=int, default=3, help="回看月数（--start 未传时生效）")
    args = ap.parse_args()

    pool = select_active_pool(
        AKShareClient(), AKSHARE_CONFIG["top_sectors"], AKSHARE_CONFIG["active_pool_size"]
    )
    if not pool:
        print(f"活跃池空（融资融券未拉到），使用预设兜底池 {len(_FALLBACK_POOL)} 只")
        pool = _FALLBACK_POOL
    sync_jqdata_1min(
        pool, months=args.months, freq=args.freq, start=args.start, end=args.end
    )
