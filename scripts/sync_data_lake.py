"""数据湖批量同步 CLI：全市场（剔除 ST/退市）过去 N 年日线【前复权】OHLCV。

关键正确性：
- 前复权用 pro_bar(adj='qfq')，【不可】用 pro.daily()（后者不复权）。
  Why 复权一致性：data/fetcher.py 的 _fetch_ohlcv_from_api 走 pro.daily() 拿的是【不复权】
  原始价，回测里拼接历史会因除权除息出现断崖跳变，导致策略信号失真。数据湖要求【前复权】
  全历史同口径，故必须用 pro.pro_bar(adj='qfq') 重算历史价，与 fetcher 不可复用。
- 断点续传：每标的独立落 shard（data_lake/shards/{ts_code}.parquet），已存在则跳过。
  Why 全市场 5000+ 标的逐只拉取耗时数小时，若中途因限频/断线失败，重跑必须从断点继续，
  不能每次从头再来——shard 粒度即最小续传单位。
- 复用 tushare_rate_limiter / tushare_breaker 防封；空数据跳过不中断。
  Why Tushare 对 pro_bar 有严格 QPS 限制，连环超限会被封 IP/账号；复用 data/resilience.py
  的令牌桶（匀速补令牌）+ 熔断器（连续失败 OPEN 期间不触达，防连环打满），与 fetcher 共享
  单例，避免重复建桶。空数据（停牌/退市/无行情）属正常业务态，不抛异常、不计熔断、不中断
  全量同步，仅跳过该只继续下一只。

用法：
    python scripts/sync_data_lake.py --years 10 --out data_lake/a_shares_daily.parquet
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta

import pandas as pd
from tqdm import tqdm

from config import LAKE_CONFIG
from data.resilience import tushare_breaker, tushare_rate_limiter


# tushare 延迟导入，避免无 token 环境直接崩。
# Why 显式隔离：本模块在 import 期不触达 tushare，单测注入 _FakePro 即可，CI/无 token
# 开发机 import 本模块也不报错；真正需要 pro_api 时才在 main()/_get_pro() 内 import。
def _get_pro():
    import tushare as ts
    from config import get_credential
    ts.set_token(get_credential("tushare", "token"))
    return ts.pro_api()


def load_universe(pro) -> list[str]:
    """全市场在售标的，剔除名称含 'ST'/'退' 的。

    Why 过滤 ST/退市：ST/*ST 股有 5% 涨跌幅限制、流动性差、退市风险高，策略层一般
    不持仓；名称含 '退' 表示已进入退市整理期，行情异常。在同步期就剔除，避免脏标的
    进入数据湖污染截面统计。
    """
    df = pro.stock_basic(list_status="L",
                         fields="ts_code,symbol,name,list_date")
    # 名称含 ST（含 *ST）或"退"字的剔除；na=False 防 NaN 名称导致 contains 报错。
    mask = (~df["name"].str.contains("ST", na=False)) & \
           (~df["name"].str.contains("退", na=False))
    return df.loc[mask, "ts_code"].tolist()


def fetch_qfq(pro, ts_code: str, start: str, end: str) -> pd.DataFrame:
    """拉取前复权日线，洗净为标准 schema。失败/空数据返回空 DF。

    限频 + 熔断手动 API 路径：
      1. acquire 令牌（阻塞至令牌桶补够，防突发打满 Tushare QPS 限频）；
      2. allow_request 熔断前置检查（OPEN 期间直接返回空，不触达 API，防连环封禁）；
      3. 失败 record_failure（连续 3 次熔断）、空数据不计熔断、成功 record_success。
    """
    tushare_rate_limiter.acquire(1.0)
    if not tushare_breaker.allow_request():
        # 熔断 OPEN：返回空，跳过该只——上层 main() 遇空 DF 即 continue 不中断。
        return pd.DataFrame()
    try:
        # adj='qfq' 前复权：全历史价按最新除权基准重算，保证序列连续无除权断崖。
        # 日期入参 strip 掉 '-'（pro_bar 要求 YYYYMMDD 整型串）。
        raw = pro.pro_bar(ts_code=ts_code, adj="qfq",
                          start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                          freq="D")
    except Exception:
        # 基础设施异常（限频 429 / 超时 / 断线）：计熔断、返回空，由上层决定是否重试。
        tushare_breaker.record_failure()
        return pd.DataFrame()
    if raw is None or raw.empty:
        # 空数据 = 正常业务态（停牌区间/无行情/退市），不抛、不计熔断、直接返回空。
        return pd.DataFrame()
    tushare_breaker.record_success()
    raw = raw.copy()
    # trade_date 入参为 YYYYMMDD 字符串，转 datetime 作为时序索引，便于后续 MultiIndex 合并。
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], format="%Y%m%d")
    raw = raw.set_index("trade_date").sort_index()
    # vol → volume 改名：统一 OHLCV schema 命名，与下游因子/回测模块期望一致。
    raw = raw.rename(columns={"vol": "volume"})
    # 列洗净：仅保留标准 6 列，丢弃 pro_bar 额外返回的 change/pct_chg/pre_close 等噪声列。
    cols = ["open", "high", "low", "close", "volume", "amount"]
    return raw[[c for c in cols if c in raw.columns]]


def build_multiindex(shard_dir: str, out: str) -> None:
    """合并所有 shard → MultiIndex(date, symbol) → pyarrow 写超级大表。

    Why MultiIndex(date, symbol)：DataLakeReader 按 date 截面 xs 切片、按 symbol 时序 xs
    切片均依赖此层级名；sort_index 让 .loc[start:end] 边界解析成立（否则 non-monotonic
    抛 KeyError）。date 列必须落盘为 datetime（非 str），保证 T6 DataLakeReader 的
    datetime 分支正常工作（_norm_date 归一化依赖 datetime dtype）。
    """
    frames = []
    for f in os.listdir(shard_dir):
        if not f.endswith(".parquet"):
            continue
        ts_code = f.replace(".parquet", "")
        df = pd.read_parquet(os.path.join(shard_dir, f))
        df["symbol"] = ts_code
        # 兼容 shard 里 index 为 trade_date 或已为 date 的情况，统一拉平为 date 列。
        df = df.reset_index().rename(columns={"trade_date": "date", "index": "date"})
        # 兼容 shard 里 index 已是 date 的情况（reset_index 后列名就是 'index'/'date'）
        if "date" not in df.columns:
            df = df.reset_index().rename(columns={"index": "date"})
        frames.append(df)
    if not frames:
        raise RuntimeError(f"shard 目录无数据：{shard_dir}")
    big = pd.concat(frames, ignore_index=True)
    # 保证 date 列为 datetime64[ns]：concat 后可能因 shard 异构 dtype 退化成 object，
    # 强制 to_datetime 收敛，落盘后层级即 datetime，符合 T6 DataLakeReader 假设。
    big["date"] = pd.to_datetime(big["date"])
    big = big.set_index(["date", "symbol"]).sort_index()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # pyarrow engine：列式写，DataLakeReader 启动期 read_parquet 内存友好。
    big.to_parquet(out, engine="pyarrow")
    print(f"数据湖写入完成：{out}，{len(big)} 行")


def main(years: int, out: str, resume: bool = True) -> None:
    """全量同步入口：拉 universe → 逐只 fetch_qfq 落 shard → 合并超级表。"""
    pro = _get_pro()
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    shard_dir = LAKE_CONFIG["shard_dir"]
    os.makedirs(shard_dir, exist_ok=True)
    codes = load_universe(pro)
    print(f"待同步标的数：{len(codes)}，区间 {start} ~ {end}")
    for ts_code in tqdm(codes):
        shard = os.path.join(shard_dir, f"{ts_code}.parquet")
        if resume and os.path.exists(shard):
            continue  # 断点续传：已落盘的跳过，重跑从断点继续。
        df = fetch_qfq(pro, ts_code, start, end)
        if df.empty:
            continue  # 停牌/退市/空 → 跳过不中断全量同步。
        df.to_parquet(shard)
        time.sleep(0.2)  # 节流：令牌桶外再叠加 200ms 间隔，双保险防 Tushare 封禁。
    build_multiindex(shard_dir, out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A 股全市场前复权日线数据湖同步")
    ap.add_argument("--years", type=int, default=LAKE_CONFIG["years_default"])
    ap.add_argument("--out", default=LAKE_CONFIG["default_path"])
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()
    main(years=args.years, out=args.out, resume=not args.no_resume)
