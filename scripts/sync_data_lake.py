"""数据湖批量同步 CLI：全市场（剔除 ST/退市）过去 N 年日线【前复权】OHLCV。

数据源：tushare 直连（pro.daily + pro.adj_factor），手动重建前复权。
2026-07-24 废弃 tnskhdata 代理后纯直连 tushare 官方 API（积分充足）。

Why 不用 pro_bar：早期代理口径下 pro_bar 不走代理 token；现纯直连后 pro_bar 仍可走，
但 daily + adj_factor 重建路径已验证稳定可用（绕过 AKShare 网络瞬态），保持不变：
price_qfq = price_raw × adj_factor / adj_factor_latest。

前复权公式：以区间最新交易日为基准（adj_factor_latest），历史价 = 原始价 × adj_factor / latest。
基准日（最新）价 = 原始价（adj/adjj_latest = 1），历史价向下调整消除除权断崖，与 pro_bar(qfq) 同语义。

关键正确性：
- 断点续传：每标的独立落 shard，已存在则跳过。全市场 5000+ 标的 × 2 请求（daily + adj_factor）
  ≈ 10000 请求 ~2.8h（1QPS），中途失败重跑从断点继续。
- 限频 + 熔断：tushare_rate_limiter + tushare_breaker；空数据跳过不中断。

用法：
    python scripts/sync_data_lake.py --years 10            # 全市场 10 年（~2.8h）
    python scripts/sync_data_lake.py --years 2 --limit 10  # 小样本调试
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta

# 加项目根到 sys.path：脚本可从任意 cwd 直接 `python scripts/xxx.py` 运行。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from tqdm import tqdm

logger = logging.getLogger(__name__)

from config import LAKE_CONFIG
from data.resilience import tushare_breaker, tushare_rate_limiter
from data._tushare_compat import get_pro, source_name


def load_universe(pro, include_delisted: bool = True) -> list[str]:
    """全市场标的（含退市，消除幸存者偏差）。

    返回的 ts_code 已带 .SH/.SZ 后缀（Tushare 格式，与 daily 湖 symbol 一致）。

    Why 含退市（include_delisted=True 默认，2026-07-19 修正幸存者偏差）：
        之前 list_status='L' 只取在上市，a_shares_daily 实测 4976 标的全活到 2026，
        退市标的（~338）被排除 → 幸存者偏差（回测系统性高估，近年段尤甚：
        个股等权近年15.6%（幸存者）vs sw指数2.3%，差13点）。含 list_status='D'
        退市标的，覆盖其退市前日线，让颈线法等回测覆盖退市标的（退市前暴跌的标的
        进池子，反映真实亏损），消除偏差。

    剔除口径：
        在市(L)：剔名称含 ST/退（防 ST/退干扰实盘池）；
        退市(D)：全保留（名称含'退'是退市标的的正常命名，不能剔，否则又回幸存者偏差）。
    """
    df_L = pro.stock_basic(list_status="L", fields="ts_code,symbol,name")
    df_L = df_L[
        (~df_L["name"].str.contains("ST", na=False))
        & (~df_L["name"].str.contains("退", na=False))
    ]
    codes = df_L["ts_code"].tolist()
    if include_delisted:
        df_D = pro.stock_basic(list_status="D", fields="ts_code,symbol,name")
        codes += df_D["ts_code"].tolist()
    return codes


def _fetch_with_guard(pro, api_name: str, **kwargs) -> pd.DataFrame:
    """限频 + 熔断 + 异常分类包装的 pro 接口调用，空数据/失败返空 DF。

    瞬时态（限频/超时/断线）计熔断；持久态（积分/权限）仅记日志；空数据不计熔断。
    """
    tushare_rate_limiter.acquire(1.0)
    if not tushare_breaker.allow_request():
        return pd.DataFrame()
    try:
        df = getattr(pro, api_name)(**kwargs)
    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ("limit", "429", "timeout", "connection", "频率", "超时", "频繁")):
            tushare_breaker.record_failure()
            logger.error("Tushare %s 限频/网络异常 [%s]：%s", api_name, kwargs.get("ts_code"), e)
        elif any(k in str(e) for k in ("积分", "权限")):
            logger.error("Tushare %s 积分不足 [%s]：%s", api_name, kwargs.get("ts_code"), e)
        else:
            tushare_breaker.record_failure()
            logger.error("Tushare %s 拉取失败 [%s]：%s", api_name, kwargs.get("ts_code"), e)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    tushare_breaker.record_success()
    return df


def fetch_qfq(pro, ts_code: str, start: str, end: str) -> pd.DataFrame:
    """pro.daily + pro.adj_factor 重建前复权日线。空数据/失败返空 DF。

    前复权：price_qfq = price_raw × adj_factor / adj_factor_latest（latest = 区间最新）。
    绕过 pro_bar（走直连不可用），daily + adj_factor 都走 pro_api 代理，稳定。
    volume/amount 不复权（除权不影响成交额口径）。
    """
    sd, ed = start.replace("-", ""), end.replace("-", "")
    raw = _fetch_with_guard(pro, "daily", ts_code=ts_code, start_date=sd, end_date=ed)
    if raw.empty:
        return pd.DataFrame()
    af = _fetch_with_guard(pro, "adj_factor", ts_code=ts_code, start_date=sd, end_date=ed)
    if af.empty:
        return pd.DataFrame()  # 无复权因子，无法重建前复权
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], format="%Y%m%d")
    af["trade_date"] = pd.to_datetime(af["trade_date"], format="%Y%m%d")
    df = raw.sort_values("trade_date").merge(
        af[["trade_date", "adj_factor"]], on="trade_date", how="left"
    )
    # 前复权基准 = 区间最新 adj_factor（按 trade_date 升序后取末值）
    af_values = df["adj_factor"].dropna()
    if af_values.empty:
        return pd.DataFrame()
    latest_af = af_values.iloc[-1]
    if pd.isna(latest_af) or latest_af == 0:
        latest_af = 1.0
    # 价格列前复权（open/high/low/close）；volume/amount 保持原值
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = df[col] * df["adj_factor"] / latest_af
    df = df.set_index("trade_date").sort_index()
    df = df.rename(columns={"vol": "volume"})  # 统一 OHLCV schema
    cols = ["open", "high", "low", "close", "volume", "amount"]
    return df[[c for c in cols if c in df.columns]]


def build_multiindex(shard_dir: str, out: str) -> None:
    """合并所有 shard → MultiIndex(date, symbol) → pyarrow 写超级大表。

    Why MultiIndex(date, symbol)：DataLakeReader 按 date 截面 xs、按 symbol 时序 xs 均依赖
    此层级名；sort_index 让 .loc[start:end] 边界解析成立。date 列落盘为 datetime。
    """
    frames = []
    for f in os.listdir(shard_dir):
        if not f.endswith(".parquet"):
            continue
        symbol = f.replace(".parquet", "")
        df = pd.read_parquet(os.path.join(shard_dir, f))
        df["symbol"] = symbol
        df = df.reset_index().rename(columns={"trade_date": "date", "index": "date"})
        if "date" not in df.columns:
            df = df.reset_index().rename(columns={"index": "date"})
        frames.append(df)
    if not frames:
        raise RuntimeError(f"shard 目录无数据：{shard_dir}")
    big = pd.concat(frames, ignore_index=True)
    big["date"] = pd.to_datetime(big["date"])
    big = big.set_index(["date", "symbol"]).sort_index()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    big.to_parquet(out, engine="pyarrow")
    print(f"数据湖写入完成：{out}，{len(big)} 行，{big.index.get_level_values('symbol').nunique()} 标的")


def main(years: int, out: str, resume: bool = True, limit: int | None = None) -> None:
    """全量同步入口：get_pro → load_universe → 逐只 fetch_qfq 落 shard → 合并超级表。"""
    pro = get_pro()
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    shard_dir = LAKE_CONFIG["shard_dir"]
    os.makedirs(shard_dir, exist_ok=True)
    codes = load_universe(pro)
    if limit:
        codes = codes[:limit]
    print(f"待同步标的数：{len(codes)}，区间 {start} ~ {end}"
          f"（源={source_name()} daily+adj_factor 前复权重建）")
    for ts_code in tqdm(codes):
        shard = os.path.join(shard_dir, f"{ts_code}.parquet")
        if resume and os.path.exists(shard):
            continue  # 断点续传：已落盘的跳过
        df = fetch_qfq(pro, ts_code, start, end)
        if df.empty:
            continue  # 停牌/退市/空 → 跳过不中断
        df.to_parquet(shard)
        time.sleep(0.2)  # 令牌桶外双保险节流
    build_multiindex(shard_dir, out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="A 股全市场前复权日线数据湖同步（代理 daily+adj_factor 重建）"
    )
    ap.add_argument("--years", type=int, default=LAKE_CONFIG["years_default"])
    ap.add_argument("--out", default=LAKE_CONFIG["default_path"])
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="仅同步前 N 只标的（小样本调试；全量留空）")
    args = ap.parse_args()
    main(years=args.years, out=args.out, resume=not args.no_resume, limit=args.limit)
