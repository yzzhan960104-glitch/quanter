"""A股日线日频增量同步：分页拉新交易日 raw daily + adj_factor，重建前复权，append 到 a_shares_daily。

Why 此脚本存在（数据底座缺口）：
  - sync_data_lake.py 是全量初始化（按标的轮询 5000×2请求 ~2.8h，不适合每日）
  - sync_incremental.py 的 quick 批不含 daily（A股日线由 sync_data_lake 写，原无日频调度）
  本脚本用 pro.daily(trade_date) + pro.adj_factor(trade_date) 分页批量（limit=500 绕过
  全市场单次大响应 ConnectionReset），2 天增量 ≈ 22 请求秒级，补 daily 日频增量缺口。

前复权一致性（与 sync_data_lake.fetch_qfq 同语义）：
  price_qfq = price_raw × adj_factor / adj_latest（adj_latest = 该标的最新交易日 adj_factor）
  除权标的（adj 在新窗口变化）的历史 qfq 自动全量重算（⑥ _recompute_symbol），
  用新窗口最新 adj 重建历史基线，消除除权断崖（守颈线法形态识别不被除权扰动误导）。

用法：
  python data/tools/sync_daily_incremental.py     # 自动读 a_shares_daily 最新日 d0，拉 [d0+1, today]
退出码：0=成功/已最新；1=失败。
"""
from __future__ import annotations
import sys
import os
import logging

# 三层 dirname：sync_daily_incremental.py → tools → data → quanter（项目根）。
# 历史 bug：两层 dirname → root=E:\quanter\data（错位），脚本模式下 sys.path 无 cwd 兜底，
# `import data` 找不到 E:\quanter/data 包 → ModuleNotFoundError（与 commit 049db6ce
# 及 smoke_trading_engine.py 同类 tools 路径少算一层 bug）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from datetime import datetime
from data._tushare_compat import get_pro
# 复用 tushare_sync 统一限频守卫：basic 桶 500/min + 熔断三态退避（_recompute_symbol
# per-symbol 全历史调用走 basic 桶；P2 防新增配额路径绕过限频触发 Tushare 限流封禁）。
from data.tushare_sync import _fetch_with_guard
# 写入前历史行数守卫（T13-A）：append 落盘前防御性校验，捕获 dedup/recompute bug 致异常收缩。
from data.integrity import safe_overwrite

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LAKE = "data_lake/a_shares_daily.parquet"
PRICE_COLS = ["open", "high", "low", "close"]
OUT_COLS = ["open", "high", "low", "close", "volume", "amount"]
PAGE = 500  # 分页大小：全市场 5530 行单次返会 ConnectionReset，500 分页稳定


def _fetch_paged(pro, api: str, trade_date: str) -> pd.DataFrame:
    """分页拉某接口某日全市场（trade_date + limit=500 + offset 累加，直到返回 < limit）。

    限频守卫（T13-B #5）：每页前 acquire basic 桶令牌（500/min），防 repair 多日补采 +
    sync 增量连续分页撞 Tushare 限频封禁。repair 裸调 pro 的历史漏洞随本函数统一收口
    （_fetch_paged 被 sync_daily_incremental + repair_gaps 共用，一处改两处受益）。
    详见 docs/superpowers/specs/2026-08-11-t13-b-scan-repair-loop-design.md。
    """
    # 延迟 import：data.resilience 无反向依赖，但保模块加载顺序清晰 + 避免顶部 import 扩散
    from data.resilience import tushare_rate_limiter_basic
    frames, offset = [], 0
    while True:
        # 每页限频（T13-B #5）：500/min basic 桶令牌，防连续分页撞 Tushare 限频封禁
        tushare_rate_limiter_basic.acquire(1.0)
        df = getattr(pro, api)(trade_date=trade_date, limit=PAGE, offset=offset)
        if df is None or df.empty:
            break
        frames.append(df)
        if len(df) < PAGE:
            break
        offset += PAGE
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# W2-2：真实时钟 import 期绑定——测试 patch datetime 类/模块属性均不影响闸钟；
# 闸自身的测试经 _intraday_guard(now=...) 显式注入。
_REAL_NOW = datetime.now
# W2-5：除权重算失败 pending sidecar（原子写；下轮 sync 优先重试）
_PENDING_RECOMPUTE = "data_lake/.syncing/pending_recompute.json"


def _load_pending_recompute() -> list[str]:
    """读上轮除权重算失败清单（缺文件/损坏返 []——sidecar 是加速重试的账本，
    读失败不阻断主流程，只是丢一轮重试）。"""
    import json
    from pathlib import Path
    p = Path(_PENDING_RECOMPUTE)
    try:
        if not p.exists():
            return []
        return list(dict.fromkeys(json.loads(p.read_text(encoding="utf-8"))))
    except Exception as e:
        logger.warning("pending_recompute sidecar 读失败（忽略，视为无待重试）：%s", e)
        return []


def _save_pending_recompute(symbols: list[str]) -> None:
    """落失败清单（tmp+replace 原子写；空清单=清账）。"""
    import json
    import os
    import tempfile
    from pathlib import Path
    p = Path(_PENDING_RECOMPUTE)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not symbols:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".pending_recompute.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sorted(set(symbols)), f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    logger.warning("除权重算失败 %d 只已记 pending sidecar（下轮 sync 优先重试）：%s",
                   len(set(symbols)), sorted(set(symbols))[:10])


def _intraday_guard(allow: bool, now=None) -> None:
    """W2-2 交易时段硬闸：工作日 09:15–15:05 拒跑（防盘中/未收盘同步把半截 bar
    写湖——d0>=today 短路让当日行永不修复，是 P0-3 的触发面）。周末全天放行；
    allow=True 是人工紧急补数逃生口。17:30 pipeline 与跨 18:00 补跑天然在窗外。
    now 参数：测试注入固定时刻（生产缺省 _REAL_NOW()）。"""
    if allow:
        return
    n = now if now is not None else _REAL_NOW()
    if n.weekday() >= 5:
        return
    hm = n.strftime("%H:%M")
    if "09:15" <= hm < "15:05":
        raise RuntimeError(
            f"交易时段拒跑（{n:%Y-%m-%d %H:%M} 工作日盘中）：tushare daily 盘中返回"
            "部分标的/现价 close，半截 bar 落湖后 d0>=today 短路永不修复。"
            "紧急补数用 --allow-intraday 显式越过。")


def _notify_critical_sync_fail(exc: Exception) -> None:
    """同步失败 CRITICAL 告警（W2-1 评审收口，2026-08-28）。

    方案 §W2-1 明文「CRITICAL 告警（走 W0 notifier）+非零退出」——原实现只有
    raise/exit1，data_pipeline 侧仅 print，adj 完整性闸触发后无人知晓（闸本身硬，
    告警面没闭合）。best-effort：notifier 不可用绝不二次炸主链（print 兜底进
    schtasks 日志，与 ops/gm_ops_common.notify 同哲学但此处直接内联——data 工具
    不 import ops 包，防跨层依赖）。"""
    try:
        import asyncio
        from infra.notifier import build_default_manager
        asyncio.run(build_default_manager().notify_risk_event(
            f"日线增量同步失败：{type(exc).__name__}: {exc}——data_ready 将不就绪，"
            f"eod/信号面当日停摆；排查 adj 接口/时段闸后重跑（--refetch-today 可修半截）",
            "CRITICAL"))
    except Exception as e:   # 通道故障降级 print（不掩盖原始失败）
        print(f"[sync CRITICAL 降级 print] 增量同步失败 {exc!r}（notify 亦失败：{e!r}）")


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
    # adj 校验（P1-A 修复）：adj 缺失/空/缺列 → 返空跳过该标的，避免下游
    # adj[["ts_code",...]] 抛 KeyError 中断整批，或 merge how="left" 后
    # adj_factor=NaN 导致价格×NaN 把整段历史写成 NaN 污染湖。
    if (adj is None or adj.empty
            or not {"ts_code", "trade_date", "adj_factor"}.issubset(adj.columns)):
        logger.warning("除权标的 %s adj_factor 空响应/缺列，跳过重算", symbol)
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


def sync_daily_incremental(no_backscan: bool = False, no_recompute_div: bool = False,
                           refetch_today: bool = False,
                           allow_intraday: bool = False) -> str:
    """增量同步入口：读 d0 → 拉新交易日 raw daily + adj_factor → 前复权 → append 落盘。

    no_backscan=True 禁用规则5近期连续性回扫（调试用；生产默认开启回扫防缺口累积）。
    no_recompute_div=True 禁用除权标的历史 qfq 全量重算（调试用；生产默认开启消除除权断崖）。
    refetch_today=True（W2-2，2026-08-28 评审）：d0==today 时强制重取当日（半截 bar
        修复口——dedup keep="last" 覆盖旧行；配合交易时段闸防盘中再写半截）。
    allow_intraday=True：越过 W2-2 交易时段硬闸（人工紧急补数逃生口）。

    W2 硬闸（2026-08-28 评审 P0-3 三件，本函数内联两件）：
      ① adj 完整性：merge 后任一行 adj_factor/latest_adj 为 NaN → 整批拒落盘 +
         raise（下一轮从 d0 重取自愈）。Why 整批而非剔行：剔行留日级缺口比失败更难
         察觉（integrity 只按日期在场判完整）；NaN 价格落湖后被 d0>=today 短路
         永不修复才是事故本体。
      ② 交易时段闸：工作日 09:15-15:05 拒跑（盘中 tushare daily 返回部分标的/现价
         close → 半截 bar 落湖且当日短路不再修复）。17:30 pipeline 与跨 18:00 补跑
         不受影响；周末全天放行。
    """
    _intraday_guard(allow_intraday)
    df = pd.read_parquet(LAKE)
    d0 = str(pd.Timestamp(df.index.get_level_values("date").max()).date())
    today = datetime.today().strftime("%Y-%m-%d")
    if d0 >= today:
        if d0 == today and refetch_today:
            logger.warning("refetch_today：重取当日 %s（dedup keep=last 覆盖旧行）", today)
        else:
            return f"已最新 {d0}，无需同步"
    # 延迟 get_pro：d0 已最新时不触发 tushare token 解析 + 模块 import（显式边界，
    # 节假日空跑不应无谓加载重依赖；守 Karpathy「彻底掌控执行环境」哲学）。
    pro = get_pro()
    days = _trade_days(pro, d0, today)
    if d0 == today and refetch_today:
        days = [today.replace("-", "")]        # 重取模式：当日即目标日
    if not days:
        return f"无新交易日（d0={d0} today={today}，可能节假日）"
    logger.info("增量同步 %s → %s，新交易日 %s", d0, today, days)

    # ① 分页拉 adj_factor [d0, today]（含 d0 作除权检测锚 + 新日期作前复权 latest）；
    # 去重守卫（W2-2）：refetch_today 模式下 days 含 d0 自身，不去重会同日双拉 →
    # adj 重复行 → 除权检测 set_index 后 Series 索引歧义 + merge 重复行。
    adj_frames = []
    for td in sorted(set([d0.replace("-", "")] + days)):
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
    # W2-1（2026-08-28 评审 P0-3）adj 完整性硬闸：任一行 adj_factor/latest_adj NaN
    # → 整批拒落盘 raise（下一轮从 d0 重取自愈）。对齐 _recompute_symbol 的 P1-A
    # 守卫（同模块内两口径必须一致——主路径裸奔是事故本体）。NaN 样本进日志供排障。
    _nan_adj = merged["adj_factor"].isna() | merged["latest_adj"].isna()
    if _nan_adj.any():
        _bad = merged[_nan_adj][["symbol", "trade_date"]].head(10)
        raise RuntimeError(
            f"adj 完整性闸触发：{int(_nan_adj.sum())} 行 adj_factor/latest_adj 为 NaN"
            f"（top: {_bad.to_dict('records')}）——整批拒落盘，检查 adj_factor 接口"
            f"是否当日未发布/部分返回；恢复后重跑自 d0={d0} 重取")
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
    # W2-5（2026-08-28 评审 P1-5）：失败标的落 pending sidecar，下一轮 sync 优先重试
    # ——原实现失败仅 warning，同一标的新旧 qfq 基线永久混合（除权断崖残留且无人知）。
    pending_prev = _load_pending_recompute()
    div_retry = [s for s in pending_prev if s not in div_syms]
    if div_retry:
        # W2-5（评审收口）：连续失败升级——pending 里的标的本轮再失败=两轮连败，
        # 除权断崖持续在场，warning 升 critical（sidecar 兜底仍自动重试）。
        logger.warning("上轮除权重算失败 %d 只转入本轮重试：%s", len(div_retry), div_retry[:10])
    div_effective = div_syms + div_retry
    failed_recompute: list[str] = []
    if div_effective and not no_recompute_div:
        logger.warning("除权标的 %d 只，全量重算历史 qfq 基线：%s", len(div_effective), div_effective)
        for sym in div_effective:
            # 单标的异常 → warning + 记入失败清单，不阻断整批（与 docstring「不阻断
            # 整批 sync」语义对齐）；失败标的由 pending sidecar 在下一轮 sync 优先重试。
            try:
                fixed = _recompute_symbol(pro, sym, todayc)
            except Exception as e:
                logger.warning("除权标的 %s 重算异常（跳过不阻断整批，转下轮重试）：%s", sym, e)
                failed_recompute.append(sym)
                continue
            if fixed.empty:
                logger.warning("除权标的 %s 全量重算返空（停牌/退市/接口异常），转下轮重试", sym)
                failed_recompute.append(sym)
                continue
            combined = combined[combined.index.get_level_values("symbol") != sym]
            combined = pd.concat([combined, fixed])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        _save_pending_recompute(failed_recompute)
        _again = sorted(set(failed_recompute) & set(pending_prev))
        if _again:
            logger.critical("除权重算连续失败 %d 只（断崖持续在场，须人工查接口/配额）：%s",
                            len(_again), _again[:10])
    elif div_effective:
        _save_pending_recompute(div_effective)   # 显式禁用也算未完成，保留待下轮
    # 写入守卫 + 原子落盘（T13-A 防御性 + G5 原子写）：safe_overwrite 内部完成
    # 「守卫 + tmp + fsync + os.replace」原子写入，调用方不再紧跟 to_parquet（防半截损坏）。
    # append 日常 combined >= 现有放行，捕获 dedup/recompute bug 致 combined 异常收缩。
    # force=QUANTER_FORCE_WRITE=1 为人为重采逃生口。
    safe_overwrite(LAKE, combined)
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
    # 子进程 stdout UTF-8 治理（final review T7a）：父进程 ops/data_pipeline.py 的
    # force_utf8_stdout 不影响子进程 stdout，GBK 管道下 ⚠️ 等 emoji 会崩。bat 侧
    # PYTHONUTF8=1 已兜底 schtasks 路径，此处补 `-m ops.data_pipeline` 直跑缺口。
    from infra.pyio import force_utf8_stdout
    force_utf8_stdout()
    import argparse as _ap
    _ap2 = _ap.ArgumentParser(description="A 股日线日频增量同步（含规则5近期回扫 + 除权标的 qfq 全量重算）")
    _ap2.add_argument("--no-backscan", action="store_true",
                      help="禁用近期连续性回扫（调试用）")
    _ap2.add_argument("--no-recompute-div", action="store_true",
                      help="禁用除权标的历史 qfq 全量重算（调试用；生产默认开启消除除权断崖）")
    _ap2.add_argument("--refetch-today", action="store_true",
                      help="W2-2：d0==today 时强制重取当日（半截 bar 修复口，dedup 覆盖旧行）")
    _ap2.add_argument("--allow-intraday", action="store_true",
                      help="W2-2：越过交易时段硬闸（人工紧急补数逃生口）")
    _args = _ap2.parse_args()
    try:
        print(sync_daily_incremental(no_backscan=_args.no_backscan,
                                     no_recompute_div=_args.no_recompute_div,
                                     refetch_today=_args.refetch_today,
                                     allow_intraday=_args.allow_intraday))
        sys.exit(0)
    except Exception as e:
        logger.exception("增量同步失败")
        print(f"FAIL: {e}")
        _notify_critical_sync_fail(e)   # W2-1（评审收口）：闸触发无人知的告警面闭合
        sys.exit(1)
