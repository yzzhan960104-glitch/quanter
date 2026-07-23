"""A股日线日频增量同步：分页拉新交易日 raw daily + adj_factor，重建前复权，append 到 a_shares_daily。

Why 此脚本存在（数据底座缺口）：
  - sync_data_lake.py 是全量初始化（按标的轮询 5000×2请求 ~2.8h，不适合每日）
  - sync_incremental.py 的 quick 批不含 daily（A股日线由 sync_data_lake 写，原无日频调度）
  本脚本用 pro.daily(trade_date) + pro.adj_factor(trade_date) 分页批量（limit=500 绕过
  全市场单次大响应 ConnectionReset），2 天增量 ≈ 22 请求秒级，补 daily 日频增量缺口。

前复权一致性（与 sync_data_lake.fetch_qfq 同语义）：
  price_qfq = price_raw × adj_factor / adj_latest（adj_latest = 该标的最新交易日 adj_factor）
  ⚠️ 除权标的（adj 在新窗口变化）的历史基准偏移：本脚本仅 append 新日期，不重算历史；
     除权标的历史 qfq 会有除权断崖位置偏差（少数标的，颈线法形态过滤影响小，
     follow-up：全量重算除权标的修正）。

用法：
  python scripts/sync_daily_incremental.py     # 自动读 a_shares_daily 最新日 d0，拉 [d0+1, today]
退出码：0=成功/已最新；1=失败。
"""
from __future__ import annotations
import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from datetime import datetime
from data._tushare_compat import get_pro

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LAKE = "data_lake/a_shares_daily.parquet"
PRICE_COLS = ["open", "high", "low", "close"]
OUT_COLS = ["open", "high", "low", "close", "volume", "amount"]
PAGE = 500  # 分页大小：全市场 5530 行单次返会 ConnectionReset，500 分页稳定


def _fetch_paged(pro, api: str, trade_date: str) -> pd.DataFrame:
    """分页拉某接口某日全市场（trade_date + limit=500 + offset 累加，直到返回 < limit）。"""
    frames, offset = [], 0
    while True:
        df = getattr(pro, api)(trade_date=trade_date, limit=PAGE, offset=offset)
        if df is None or df.empty:
            break
        frames.append(df)
        if len(df) < PAGE:
            break
        offset += PAGE
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _trade_days(pro, d0: str, today: str) -> list[str]:
    """[d0+1, today] 的交易日列表（trade_cal 剔除 d0 + 节假日）。

    物理意图：只拉真正的新交易日，避免节假日空拉浪费请求。
    """
    cal = pro.trade_cal(exchange="SSE", start_date=d0.replace("-", ""),
                        end_date=today.replace("-", ""))
    cal = cal[cal["is_open"] == 1]
    d0c = d0.replace("-", "")
    return [str(d) for d in cal["cal_date"].tolist() if str(d) > d0c]


def sync_daily_incremental() -> str:
    """增量同步入口：读 d0 → 拉新交易日 raw daily + adj_factor → 前复权 → append 落盘。"""
    df = pd.read_parquet(LAKE)
    d0 = str(pd.Timestamp(df.index.get_level_values("date").max()).date())
    today = datetime.today().strftime("%Y-%m-%d")
    if d0 >= today:
        return f"已最新 {d0}，无需同步"
    # 延迟 get_pro：d0 已最新时不触发 tushare token 解析 + 模块 import（显式边界，
    # 节假日空跑不应无谓加载重依赖；守 Karpathy「彻底掌控执行环境」哲学）。
    pro = get_pro()
    days = _trade_days(pro, d0, today)
    if not days:
        return f"无新交易日（d0={d0} today={today}，可能节假日）"
    logger.info("增量同步 %s → %s，新交易日 %s", d0, today, days)

    # ① 分页拉 adj_factor [d0, today]（含 d0 作除权检测锚 + 新日期作前复权 latest）
    adj_frames = []
    for td in [d0.replace("-", "")] + days:
        af = _fetch_paged(pro, "adj_factor", td)
        if not af.empty:
            adj_frames.append(af)
    if not adj_frames:
        return "adj_factor 拉取为空（接口异常/权限？）"
    adj = pd.concat(adj_frames, ignore_index=True)
    adj["trade_date"] = pd.to_datetime(adj["trade_date"], format="%Y%m%d")

    # ② 分页拉 raw daily [d0+1, today]
    raw_frames = []
    for td in days:
        d = _fetch_paged(pro, "daily", td)
        if not d.empty:
            raw_frames.append(d)
    if not raw_frames:
        return "raw daily 拉取为空（接口异常？）"
    raw = pd.concat(raw_frames, ignore_index=True)
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], format="%Y%m%d")
    raw = raw.rename(columns={"ts_code": "symbol", "vol": "volume"})

    # ③ 前复权：每标的 latest adj（新窗口最新交易日）→ price_qfq = raw × adj / latest
    merged = raw.merge(
        adj[["ts_code", "trade_date", "adj_factor"]],
        left_on=["symbol", "trade_date"], right_on=["ts_code", "trade_date"],
        how="left",
    ).drop(columns=["ts_code"], errors="ignore")
    latest_adj = (merged.sort_values(["symbol", "trade_date"])
                       .groupby("symbol")["adj_factor"].last())
    merged["latest_adj"] = merged["symbol"].map(latest_adj)
    for col in PRICE_COLS:
        if col in merged.columns:
            merged[col] = merged[col] * merged["adj_factor"] / merged["latest_adj"]

    # ④ 除权检测（adj 在 [d0, today] 变化）→ 历史基准偏移标注（follow-up 全量重算）
    adj_pivot = adj.assign(td=adj["trade_date"].dt.strftime("%Y%m%d"))
    d0c, todayc = d0.replace("-", ""), today.replace("-", "")
    adj_d0 = adj_pivot[adj_pivot["td"] == d0c].set_index("ts_code")["adj_factor"]
    adj_today = adj_pivot[adj_pivot["td"] == todayc].set_index("ts_code")["adj_factor"]
    div_syms = [s for s in latest_adj.index
                if s in adj_d0.index and s in adj_today.index
                and abs(adj_d0[s] - adj_today[s]) > 1e-6]
    if div_syms:
        logger.warning("⚠️ 除权标的 %d 只（adj %s→%s 变化），历史 qfq 基准未重算（follow-up）：%s",
                       len(div_syms), d0, today, div_syms[:10])

    # ⑤ 组装新行 → MultiIndex(date, symbol) + append + 去重（保留新）+ 落盘
    new = merged[["trade_date", "symbol"] + OUT_COLS].copy().rename(columns={"trade_date": "date"})
    new["date"] = pd.to_datetime(new["date"])
    new = new.set_index(["date", "symbol"]).sort_index()
    combined = pd.concat([df, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined.to_parquet(LAKE, engine="pyarrow")
    new_d0 = str(pd.Timestamp(combined.index.get_level_values("date").max()).date())
    logger.info("完成：a_shares_daily %d 行，最新日 %s（新增 %d 行）",
                len(combined), new_d0, len(new))
    return f"OK 最新日 {new_d0}（+{len(new)} 行，除权标的 {len(div_syms)} 只待重算）"


if __name__ == "__main__":
    try:
        print(sync_daily_incremental())
        sys.exit(0)
    except Exception as e:
        logger.exception("增量同步失败")
        print(f"FAIL: {e}")
        sys.exit(1)
