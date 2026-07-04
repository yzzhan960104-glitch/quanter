"""基本面因子数据湖同步：批量按 trade_date 拉 Tushare daily_basic（全市场估值面板）。

设计核心 —— 批量按日期，非逐标的：
- 逐标的拉（fetch_factor_data 范式）= 全市场 5000 × 6 估值因子 = 30000 请求 ≈ 8h（1QPS）；
- 批量按 trade_date（pro.daily_basic(trade_date=YYYYMMDD)）= 一次返回全市场当日估值，
  请求数 = 交易日数 ≈ 2430（10年），约 40 分钟，**效率提升 12 倍**。
Why 必须批量：逐标的在限频下不可行（数小时），批量是全市场基本面落盘的唯一可行口径。

数据源：优先代理 tnskhdata（10000 积分，TNSKHDATA_TOKEN，全市场可用）；回退直连 tushare
（TUSHARE_TOKEN，daily_basic 需 2000+ 积分，普通账户可能不足）。
实测代理 10000 积分：stock_basic / daily_basic / fina_indicator / trade_cal 全部解锁，
全市场 5534 标的可拉。

落盘 schema：(date, symbol) MultiIndex × [pe, pb, ps, pe_ttm, pb_ttm, dv_ratio, total_mv, circ_mv]
与 daily 湖同形（MultiIndex(date,symbol)），DataLakeReader.get_cross_section(date, lake="fundamentals")
直接返回全市场当日估值面板，供 factors/fundamental.py 做横截面分位。

用法：
    python scripts/sync_fundamentals.py --years 10                # 全市场 10 年（需积分）
    python scripts/sync_fundamentals.py --years 2 --limit-dates 20 # 小样本（20 个交易日）
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta

# 加项目根到 sys.path（脚本可从任意 cwd 直接跑）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from tqdm import tqdm

logger = logging.getLogger(__name__)

from config import LAKE_CONFIG
from data.resilience import tushare_breaker, tushare_rate_limiter

# daily_basic 估值字段（Tushare 官方）——全市场日频，落盘后供横截面估值分位
_VALUATION_FIELDS = "ts_code,trade_date,pe,pb,ps,pe_ttm,pb_ttm,dv_ratio,total_mv,circ_mv"


def _get_pro():
    """延迟初始化 pro 接口（优先代理 tnskhdata 10000 积分，回退直连 tushare）。"""
    from data._tushare_compat import get_pro
    return get_pro()


def fetch_trade_calendar(pro, start: str, end: str) -> list[str]:
    """Tushare trade_cal 拉交易日历（is_open=1 仅交易日）。

    返回 ['YYYYMMDD', ...] 字符串列表（供 daily_basic trade_date 入参）。
    """
    tushare_rate_limiter.acquire(1.0)
    df = pro.trade_cal(exchange="SSE", start_date=start.replace("-", ""),
                       end_date=end.replace("-", ""), is_open="1")
    return df["cal_date"].tolist()


def fetch_valuation_panel(pro, trade_date: str) -> pd.DataFrame:
    """pro.daily_basic(trade_date=) 拉全市场当日估值面板（~5000 行）。

    单次请求返回全市场当日 pe/pb/ps/市值，是「批量按日期」的核心。
    限频 + 熔断与 fetch_qfq 同范式；空数据（非交易日/盘前）不中断。
    """
    tushare_rate_limiter.acquire(1.0)
    if not tushare_breaker.allow_request():
        return pd.DataFrame()
    try:
        df = pro.daily_basic(trade_date=trade_date, fields=_VALUATION_FIELDS)
    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ("limit", "429", "timeout", "connection", "频率", "超时")):
            tushare_breaker.record_failure()
            logger.error("Tushare daily_basic 限频/网络异常 [%s]：%s", trade_date, e)
        elif any(k in str(e) for k in ("积分", "权限")):
            logger.error("Tushare daily_basic 积分不足 [%s]：%s", trade_date, e)
        else:
            tushare_breaker.record_failure()
            logger.error("Tushare daily_basic 拉取失败 [%s]：%s", trade_date, e)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    tushare_breaker.record_success()
    return df


def build_fundamentals_panel(panels: list[pd.DataFrame]) -> pd.DataFrame:
    """合并逐日面板 → MultiIndex(date, symbol) × 估值列，date 为 datetime。"""
    if not panels:
        raise RuntimeError("基本面面板为空（全部交易日拉取失败/积分不足）")
    big = pd.concat(panels, ignore_index=True)
    big["trade_date"] = pd.to_datetime(big["trade_date"], format="%Y%m%d")
    big = big.rename(columns={"ts_code": "symbol", "trade_date": "date"})
    big = big.set_index(["date", "symbol"]).sort_index()
    # 数值列 coerce（Tushare 偶返回 None/字符串，落盘前强制数值化）
    for col in ["pe", "pb", "ps", "pe_ttm", "pb_ttm", "dv_ratio", "total_mv", "circ_mv"]:
        if col in big.columns:
            big[col] = pd.to_numeric(big[col], errors="coerce")
    return big


def sync_fundamentals(years: int, out: str, limit_dates: int | None = None) -> None:
    """全市场估值因子同步入口：交易日历 → 逐日 daily_basic → 合并落盘。"""
    pro = _get_pro()
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    trade_dates = fetch_trade_calendar(pro, start, end)
    if limit_dates:
        trade_dates = trade_dates[:limit_dates]
    print(f"待同步交易日数：{len(trade_dates)}，区间 {start} ~ {end}（源=Tushare daily_basic 批量）")

    panels: list[pd.DataFrame] = []
    for td in tqdm(trade_dates):
        df = fetch_valuation_panel(pro, td)
        if not df.empty:
            panels.append(df)
        time.sleep(0.2)  # 令牌桶外双保险节流

    if not panels:
        print("全部交易日拉取失败（积分不足/网络），未落盘。请检查 Tushare 积分（daily_basic 需 2000+）。")
        return

    big = build_fundamentals_panel(panels)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    big.to_parquet(out, engine="pyarrow")
    print(f"基本面湖写入完成：{out}，{len(big)} 行，"
          f"{big.index.get_level_values('symbol').nunique()} 标的，"
          f"{big.index.get_level_values('date').nunique()} 交易日")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A 股全市场基本面因子（估值）数据湖同步")
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--out", default=LAKE_CONFIG["lakes"]["fundamentals"])
    ap.add_argument("--limit-dates", type=int, default=None,
                    help="仅同步前 N 个交易日（小样本调试）")
    args = ap.parse_args()
    sync_fundamentals(years=args.years, out=args.out, limit_dates=args.limit_dates)
