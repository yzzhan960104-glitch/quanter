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
  python data/tools/sync_daily_incremental.py     # 自动读 a_shares_daily 最新日 d0，拉 [d0+1, today]
退出码：0=成功/已最新；1=失败。
"""
from __future__ import annotations
import sys
import os
import logging

# 三层 dirname：sync_daily_incremental.py → tools → data → quanter（项目根）。
# 历史 bug：两层 dirname → root=F:\quanter\data（错位），脚本模式下 sys.path 无 cwd 兜底，
# `import data` 找不到 F:\quanter/data 包 → ModuleNotFoundError（与 commit 049db6ce
# 及 smoke_trading_engine.py 同类 tools 路径少算一层 bug）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from datetime import datetime
from data._tushare_compat import get_pro
# 复用 tushare_sync 统一限频守卫：basic 桶 500/min + 熔断三态退避（_recompute_symbol
# per-symbol 全历史调用走 basic 桶；P2 防新增配额路径绕过限频触发 Tushare 限流封禁）。
from data.tushare_sync import _fetch_with_guard

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


def _backscan_recent(df, trade_days_set, suspend_intervals, days=30):
    """抽查 lake 近 days 个交易日连续性，返 unjustified gaps（规则 5 回扫）。

    物理意图：sync 增量只补 d0→today，d0 之前的近期缺口不补；回扫抽查近期连续性，
    发现漏采则告警/触发 repair_gaps（历史远期缺口由 scan_integrity 一次性兜底）。

    Args:
        df: lake df（MultiIndex(date, symbol)）。
        trade_days_set: 全期交易日集合。
        suspend_intervals: load_suspend_intervals 输出。
        days: 近期交易日窗口（默认 30，覆盖停牌复牌常见周期）。
    Returns:
        list[GapRange]（仅 unjustified，含至少一个非停牌漏采日）。
    """
    from data.integrity import find_gaps
    # 取 df 最近 days 个唯一交易日（date 层级），截近期子集扫缺口
    recent_dates = sorted(df.index.get_level_values("date").unique())[-days:]
    if not recent_dates:
        return []
    recent_df = df[df.index.get_level_values("date").isin(recent_dates)]
    gaps = find_gaps(recent_df, trade_days_set, suspend_intervals)
    return [g for g in gaps if not g.suspend_justified]


def _recompute_symbol(pro, symbol: str, todayc: str) -> pd.DataFrame:
    """按标的拉全历史 raw + adj，用窗口最新 adj 重建 qfq，返 MultiIndex(date, symbol)。

    物理意图：除权事件后，旧 qfq 基准（旧 latest_adj）失效，历史行停留在旧基线会形成
    除权断崖（close 跳空）；本函数按新窗口最新 adj_factor 重算全历史，把基准拉到最新日，
    消除断崖，使 detect_signal 形态识别不被除权扰动误导。

    前复权公式（与 sync_data_lake.fetch_qfq 同语义）：
        price_qfq = price_raw × adj_factor / latest_adj（latest_adj = 窗口最新交易日 adj）

    Args:
        pro: tushare pro 接口（保留参数语义对齐 fetch_qfq(pro, ...)，实际通过
            _fetch_with_guard 内部 get_pro() 解析；显式传 pro 仅为 API 形态一致）。
        symbol: 标的代码（如 000001.SZ）。
        todayc: 窗口截止日（YYYYMMDD，不含连字符）。
    Returns:
        MultiIndex(date, symbol) DataFrame；raw 拉空（停牌/退市/接口异常）返空 DF，
        不抛异常（守数据底座鲁棒性——单只除权标的失败不应阻断整批 sync）。

    起点 19900101 是哨兵下限，Tushare 按上市日自动截取（老股 1990-1999 段返空属正常，
    非 bug——P3 防后人误判）。
    """
    raw = _fetch_with_guard("daily", ts_code=symbol,
                            start_date="19900101", end_date=todayc)
    adj = _fetch_with_guard("adj_factor", ts_code=symbol,
                            start_date="19900101", end_date=todayc)
    if raw is None or raw.empty:
        return pd.DataFrame()
    raw = raw.rename(columns={"ts_code": "symbol", "vol": "volume"})
    merged = raw.merge(
        adj[["ts_code", "trade_date", "adj_factor"]],
        left_on=["symbol", "trade_date"], right_on=["ts_code", "trade_date"],
        how="left",
    ).drop(columns=["ts_code"], errors="ignore")
    latest_adj = merged.sort_values("trade_date")["adj_factor"].iloc[-1]
    if pd.isna(latest_adj) or latest_adj == 0:
        latest_adj = 1.0
    for col in PRICE_COLS:
        if col in merged.columns:
            merged[col] = merged[col] * merged["adj_factor"] / latest_adj
    merged["trade_date"] = pd.to_datetime(merged["trade_date"], format="%Y%m%d")
    merged = merged.rename(columns={"trade_date": "date"})
    return merged[["date", "symbol"] + OUT_COLS].set_index(["date", "symbol"]).sort_index()


def sync_daily_incremental(no_backscan: bool = False, no_recompute_div: bool = False) -> str:
    """增量同步入口：读 d0 → 拉新交易日 raw daily + adj_factor → 前复权 → append 落盘。

    no_backscan=True 禁用规则5近期连续性回扫（调试用；生产默认开启回扫防缺口累积）。
    no_recompute_div=True 禁用除权标的历史 qfq 全量重算（调试用；生产默认开启消除除权断崖）。
    """
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

    # ④ 除权检测（adj 在 [d0, today] 变化）→ 全量重算历史 qfq 基线（消除除权断崖）
    adj_pivot = adj.assign(td=adj["trade_date"].dt.strftime("%Y%m%d"))
    d0c, todayc = d0.replace("-", ""), today.replace("-", "")
    adj_d0 = adj_pivot[adj_pivot["td"] == d0c].set_index("ts_code")["adj_factor"]
    adj_today = adj_pivot[adj_pivot["td"] == todayc].set_index("ts_code")["adj_factor"]
    div_syms = [s for s in latest_adj.index
                if s in adj_d0.index and s in adj_today.index
                and abs(adj_d0[s] - adj_today[s]) > 1e-6]
    if div_syms:
        logger.warning("⚠️ 除权标的 %d 只（adj %s→%s 变化），历史 qfq 基准将重算：%s",
                       len(div_syms), d0, today, div_syms[:10])

    # ⑤ 组装新行 → MultiIndex(date, symbol) + append + 去重（保留新）+ 落盘
    new = merged[["trade_date", "symbol"] + OUT_COLS].copy().rename(columns={"trade_date": "date"})
    new["date"] = pd.to_datetime(new["date"])
    new = new.set_index(["date", "symbol"]).sort_index()
    combined = pd.concat([df, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()

    # ⑥ 除权标的历史全量重算（默认开启；--no-recompute-div 可禁用）
    # 物理意图：步骤⑤ append 后，除权标的旧行仍停留在旧 latest_adj 基准，形成除权断崖；
    # 这里按标的拉全历史 raw+adj，用新窗口最新 adj 重算，替换该标的全部历史行。
    # ⚠️ 配额影响（P2）：per-symbol 全历史调用（1 标的 ≈ 2 次 daily/adj_factor 请求），
    # 除权季单次可能几十只；_fetch_with_guard 统一限频兜底（basic 桶 ~500/min），
    # 超时/熔断按数据集语义返空跳过该标的（不阻断整批 sync）。
    if div_syms and not no_recompute_div:
        logger.warning("除权标的 %d 只，全量重算历史 qfq 基线：%s", len(div_syms), div_syms)
        for sym in div_syms:
            fixed = _recompute_symbol(pro, sym, todayc)
            if fixed.empty:
                logger.warning("除权标的 %s 全量重算返空（停牌/退市/接口异常），跳过", sym)
                continue
            combined = combined[combined.index.get_level_values("symbol") != sym]
            combined = pd.concat([combined, fixed])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined.to_parquet(LAKE, engine="pyarrow")
    new_d0 = str(pd.Timestamp(combined.index.get_level_values("date").max()).date())
    logger.info("完成：a_shares_daily %d 行，最新日 %s（新增 %d 行）",
                len(combined), new_d0, len(new))

    # 规则5：回扫近期连续性（抽查 d0 之前近期缺口——sync 增量只补 d0→today，不补历史缺口）
    backscan_msg = ""
    if not no_backscan:
        try:
            from datetime import timedelta
            from pathlib import Path
            from data.integrity import fetch_trade_days, load_suspend_intervals
            back_start = (datetime.today() - timedelta(days=60)).strftime("%Y-%m-%d")
            back_td = fetch_trade_days(back_start, today)
            susp_path = Path("data_lake/suspend_d.parquet")
            if susp_path.exists():
                susp_df = pd.read_parquet(susp_path)
                susp = load_suspend_intervals(susp_df, back_td)
            else:
                susp = {}
            unjustified = _backscan_recent(combined, back_td, susp, days=30)
            if unjustified:
                backscan_msg = f"；⚠️ 回扫发现 {len(unjustified)} 段近期漏采，跑 repair_gaps --auto 补"
                logger.warning("sync 回扫发现 %d 段近期漏采，top 标的：%s",
                               len(unjustified), [g.symbol for g in unjustified[:10]])
        except Exception as e:
            # 回扫异常不阻断主流程（增量已落盘，回扫是附加防护）
            logger.warning("sync 回扫异常（不阻断主流程）：%s", e)

    recompute_msg = "" if (div_syms and not no_recompute_div) else (
        f"，除权标的 {len(div_syms)} 只未重算" if div_syms else "")
    return f"OK 最新日 {new_d0}（+{len(new)} 行，除权标的 {len(div_syms)} 只{recompute_msg}{backscan_msg}）"


if __name__ == "__main__":
    import argparse as _ap
    _ap2 = _ap.ArgumentParser(description="A 股日线日频增量同步（含规则5近期回扫 + 除权标的 qfq 全量重算）")
    _ap2.add_argument("--no-backscan", action="store_true",
                      help="禁用近期连续性回扫（调试用）")
    _ap2.add_argument("--no-recompute-div", action="store_true",
                      help="禁用除权标的历史 qfq 全量重算（调试用；生产默认开启消除除权断崖）")
    _args = _ap2.parse_args()
    try:
        print(sync_daily_incremental(no_backscan=_args.no_backscan,
                                     no_recompute_div=_args.no_recompute_div))
        sys.exit(0)
    except Exception as e:
        logger.exception("增量同步失败")
        print(f"FAIL: {e}")
        sys.exit(1)
