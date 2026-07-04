"""板块两融 + 活跃股初筛：融资融券明细 → 申万一级行业 groupby → 融资余额环比
增速 top3 板块 → 板块内按 20 日动量/换手选 50 只活跃股 → 拉其前复权日线。

漏斗物理意图（Why 漏斗）：
    宏观信贷扩张（Task 5 社融/M1M2/DR007）先在【板块融资余额】端显形——机构与
    杠杆资金率先涌入景气板块，融资余额环比增速领跑；随后才传导到板块内【活跃
    个股】（换手率/动量放大）。本漏斗用「融资增速 → top 板块 → 个股活跃度」两级
    过滤，把全市场 5000+ 只 A 股压缩到 ≤50 只活跃池，避免对全市场拉取日线
    （IO 爆炸、限频），也为下游分钟级因子（Task 8/12）收敛候选域。

行业映射现实拷问（降级路径，详见 select_active_pool）：
    akshare 的 fetch_margin_detail()（stock_margin_detail_sse/szse）返回【个股融资
    余额明细】，列名随版本漂移，且【未必含"行业"列】。若强行追求"完美行业映射"需
    额外调用个股信息接口，耦合复杂且不稳定（akshare 上游列名漂移频发）。故采用
    显式降级链：margin 含行业列 → 板块 groupby；否则 → 个股资金流兜底直接选 top
    个股（绕过行业）。降级路径在注释与日志中显式说明，保证逻辑可单测。
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import sys

# 加项目根到 sys.path：脚本可从任意 cwd 直接 `python scripts/xxx.py` 运行。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from config import AKSHARE_CONFIG, LAKE_CONFIG
from data.clients.akshare_client import AKShareClient

logger = logging.getLogger(__name__)


def _pick_col(df: pd.DataFrame, *keys: str) -> str | None:
    """防御性取列名（akshare 版本间中文列名漂移）。

    Why：akshare 上游接口列名漂移频发（如"融资余额" vs "融资余额(元)" vs
    "rzye"），硬编码会 KeyError。本函数按 keys 顺序：
        - 精确命中 → 返回；
        - 模糊命中（包含任一 key 子串，忽略大小写与全半角括号）→ 返回真实列名；
        - 都不命中 → None（让上游自然走降级，而非抛 KeyError 静默崩）。
    """
    norm = {c: str(c).lower().replace("（", "(").replace("）", ")") for c in df.columns}
    for k in keys:
        if k in df.columns:
            return k
        for c, nc in norm.items():
            if k in nc:
                return c
    return None


def compute_margin_growth(margin: pd.DataFrame, prev: dict[str, float]) -> pd.DataFrame:
    """按行业 groupby 算融资余额环比增速（增速降序返回）。

    漏斗第一级：把【个股融资余额】聚合到【行业】，再与昨日行业余额比，得增速。
    增速领跑的行业 = 杠杆资金正在涌入的景气板块（top 板块候选）。

    参数：
        margin: 个股融资明细（须含「行业」「融资余额」列；列名漂移由 _pick_col 容错）。
        prev:   {行业: 昨日融资余额}。真实场景从昨日 sector.parquet 读取；
                传空 dict 时按"昨日=今日"语义处理（增速为 0，不致除零）。

    前视红线 / 除零防御：
        - 除数 max(prev_val, 1.0)：昨日余额缺失时回落到 1.0，绝不除以零；
        - prev 缺某行业 → get 默认今日值，使该行业增速为 0（中性，不污染排序）。
    """
    if margin is None or margin.empty:
        return pd.DataFrame(columns=["行业", "融资余额", "growth"])
    ind_col = _pick_col(margin, "行业", "申万一级行业", "行业级别")
    bal_col = _pick_col(margin, "融资余额", "rzye", "融资余额(元)")
    if ind_col is None or bal_col is None:
        # 缺关键列：返空（让上游走降级路径，而非抛 KeyError 静默崩）
        logger.warning("compute_margin_growth 缺行业/融资余额列，返空")
        return pd.DataFrame(columns=["行业", "融资余额", "growth"])
    g = (margin.assign(_bal=pd.to_numeric(margin[bal_col], errors="coerce").fillna(0.0))
         .groupby(ind_col)["_bal"].sum().reset_index()
         .rename(columns={ind_col: "行业", "_bal": "融资余额"}))

    def _growth(row: pd.Series) -> float:
        # 增速 = (今 - 昨) / max(昨, 1)；昨缺失 → 今（增速0），除数兜底 1.0 防除零
        today = float(row["融资余额"])
        yesterday = prev.get(row["行业"], today)
        return (today - yesterday) / max(yesterday, 1.0)

    g["growth"] = g.apply(_growth, axis=1)
    # 排序稳定性：增速相同时按融资余额绝对值降序（资金体量大的板块优先，
    # 物理上更稳健，避免 pandas quicksort 不稳定排序让冷启小板块窜到前面）。
    return g.sort_values(["growth", "融资余额"], ascending=False).reset_index(drop=True)


def select_active_pool(
    client: AKShareClient,
    top_n: int = 3,
    pool_size: int = 50,
    *,
    momentum_window: int | None = None,
    prev: dict[str, float] | None = None,
) -> list[str]:
    """漏斗：融资增速 → top 板块 → 板块内个股按动量/换手选 pool_size 只。

    降级路径（行业映射现实拷问的优雅处理）：
        主路径：margin 含「行业」列 → compute_margin_growth → top_n 行业 →
                在 top 行业内按 20 日动量/成交量排序取 pool_size。
        降级 A：margin 缺「行业」列（akshare 版本漂移 / 两所未合并） →
                绕过行业，直接对所有 margin 个股按个股资金流（主力净流入）+
                动量综合排序取 pool_size（用个股资金流作为板块景气的微观代理）。
        降级 B：margin 与个股资金流都失效 → 返回空池（绝不抛，落盘跳过）。

    参数：
        client:           AKShareClient（或具同名方法的 mock）。
        top_n:            取前 N 个融资增速领跑行业（默认 config top_sectors=3）。
        pool_size:        活跃池规模（默认 50；测试用小池验证逻辑）。
        momentum_window:  动量回看窗口（默认 config momentum_window=20）。
        prev:             {行业: 昨日融资余额}，用于算环比增速；默认 None 表示
                          「昨日=今日」（增速 0，实盘首日冷启）。实盘应从昨日
                          sector.parquet 读 prev 注入；测试注入 prev 让增速有区分度。
                          传空 dict 与 None 同语义。

    返回：活跃股 symbol 列表（akshare 代码形式，如 '000001.SZ'）；空池返 []。
    """
    if momentum_window is None:
        momentum_window = AKSHARE_CONFIG.get("momentum_window", 20)

    margin = client.fetch_margin_detail()
    if margin is None or margin.empty:
        logger.warning("select_active_pool：margin 为空，走降级 B（返空池）")
        return []

    sym_col = _pick_col(margin, "标的代码", "代码", "证券代码", "symbol")
    if sym_col is None:
        logger.warning("select_active_pool：margin 无代码列，无法筛股，返空池")
        return []
    # 1) 取原始代码 + 过滤无效（NaN/空/非6位），避免脏符号失败 trip 熔断连累有效标的。
    #    显式 str() 强转：margin 代码列可能是 float（NaN/601688.0），不依赖 astype(str) 的列级行为。
    _raw_codes: list[str] = []
    for _v in margin[sym_col].tolist():
        c = str(_v).strip()
        if not c or c.lower() == "nan":
            continue
        d = c.split(".")[0]
        if d.isdigit() and len(d) == 6:
            _raw_codes.append(c)
    # 2) 性能红线：候选可能数千只，下游逐只 API（个股资金流/日线）O(N) 极慢（实测 6min+ 未完）。
    #    先按融资余额（margin 内已有，零额外 API）预筛 top 100，再交给下游逐只评分。
    bal_col = _pick_col(margin, "融资余额")
    if bal_col is not None and len(_raw_codes) > 100:
        _raw_set = set(_raw_codes)
        _sub = (
            margin[margin[sym_col].astype(str).isin(_raw_set)]
            .sort_values(bal_col, ascending=False)
            .head(100)
        )
        _raw_codes = _sub[sym_col].astype(str).tolist()
        logger.info("候选预筛：按融资余额取 top 100（从全市场缩到 100，避免逐只 API 雪崩）")
    # 3) 归一带后缀（margin 返6位纯数字，下游 JQData 需 .SZ/.SH；
    #    沪 6/9→.SH, 深 0/3/2→.SZ, 北 8/4→.BJ）
    def _suf(c: str) -> str:
        d = c.split(".")[0]
        return d + (
            ".SH" if d[0] in "69"
            else ".SZ" if d[0] in "032"
            else ".BJ" if d[0] in "84"
            else ""
        )
    candidates_all = [_suf(c) for c in _raw_codes]

    # ---- 主路径：margin 含行业列 → 行业 groupby ----
    ind_col = _pick_col(margin, "行业", "申万一级行业", "行业级别")
    if ind_col is not None:
        # prev 默认空（冷启/测试）；实盘从昨日 sector.parquet 读 {行业:昨日余额}
        growth = compute_margin_growth(margin, prev=prev or {})
        top = growth.head(top_n)["行业"].tolist()
        if not top:
            # groupby 异常（如行业列全 NaN）→ 落降级 A
            logger.warning("margin 行业 groupby 为空，走降级 A（个股资金流兜底）")
            return _select_by_individual_flow(client, candidates_all, pool_size)
        pool_set = set(margin[margin[ind_col].isin(top)][sym_col].astype(str))
        scored = _score_by_momentum(client, list(pool_set), momentum_window)
        if scored:
            return [s for s, _ in scored[:pool_size]]
        # 主路径动量全空（如全停牌）→ 降级 A 兜底
        logger.warning("top 板块内动量评分全空，走降级 A（个股资金流兜底）")
        return _select_by_individual_flow(client, list(pool_set), pool_size)

    # ---- 降级 A：margin 无行业列 → 个股资金流 + 动量综合排序 ----
    logger.info("margin 无行业列，走降级 A：个股资金流兜底选 top（绕过行业）")
    return _select_by_individual_flow(client, candidates_all, pool_size)


def _score_by_momentum(
    client: AKShareClient, symbols: list[str], window: int
) -> list[tuple[str, float]]:
    """对候选股拉近 `window` 日前复权日线 → 按（动量, 成交量）综合评分降序。

    动量 = 收盘价 pct_change().sum()（窗口内累计涨幅，正值代表多头趋势）。
    成交量均值作次序键，刻画活跃度（换手率高的票更易进出场、滑点可控）。

    Why 拉 close/volume 用英文列名：AKShareClient._cleanse_daily 已把 akshare
    中文列名洗净为英文标准 schema，故此处直接用 close/volume（与生产一致）。
    """
    end = _today()
    start = _shift(window + 5)  # +5 缓冲：避免窗口边界缺数据导致样本不足
    scored: list[tuple[str, float]] = []
    for sym in symbols:
        df = client.fetch_daily_hist(sym, start, end, adjust="qfq")
        if df is None or df.empty or len(df) < 5:
            # 样本不足（新股/停牌）→ 跳过，绝不用脏数据评分
            continue
        close = df["close"] if "close" in df.columns else df.iloc[:, 0]
        vol = df["volume"] if "volume" in df.columns else pd.Series([1] * len(df))
        mom = float(close.pct_change().sum())
        turn = float(vol.mean())
        # 综合分：动量为主，成交量作微弱加权次序键（避免高动量低流动性陷阱）
        scored.append((sym, mom + turn * 1e-8))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _select_by_individual_flow(
    client: AKShareClient, symbols: list[str], pool_size: int
) -> list[str]:
    """降级 A 兜底：margin 无行业列时，用个股资金流（主力净流入）直接选 top。

    Why 个股资金流可作板块景气代理：主力资金净流入大的个股通常属于当日热点
    板块，其集合近似 top 板块的微观投影。失败（个股资金流返空）→ 取 margin
    原始候选前 pool_size 只（不崩，至少保证有池可拉日线）。
    """
    scored: list[tuple[str, float]] = []
    for sym in symbols:
        ff = client.fetch_individual_fund_flow(sym)
        if ff is None or ff.empty:
            continue
        col = _pick_col(ff, "今日主力净流入-净额", "主力净流入-净额", "主力净流入")
        if col is None:
            continue
        val = pd.to_numeric(ff[col], errors="coerce").sum()
        scored.append((sym, float(val)))
    if scored:
        scored.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in scored[:pool_size]]
    # 兜底的兜底：个股资金流全空 → 取 margin 候选前 pool_size 只
    return symbols[:pool_size]


def sync_sector_daily(
    out_sector: str | None = None,
    out_daily: str | None = None,
    *,
    top_n: int | None = None,
    pool_size: int | None = None,
) -> None:
    """落 data_lake/sector.parquet（板块资金流）+ a_shares_active.parquet（活跃池日线）。

    数据流：
        1. select_active_pool 选 ≤pool_size 只活跃股；
        2. 板块资金流（fetch_sector_fund_flow）原样落 sector.parquet（供 CreditRegime
           / 前端驾驶舱读板块景气）；
        3. 活跃池前复权日线（近 1 年）合并落 daily_active 湖（a_shares_active.parquet），
           MultiIndex(date, symbol) 便于下游因子按 symbol groupby。

    Why 分流到 daily_active（不复用 daily 湖）：sync_data_lake 写 daily 湖（全市场×N年），
    本脚本写活跃池（~50只×1年）；若同写 a_shares_daily.parquet 会互相整表覆盖（活跃池是
    全市场稀疏子集，维度远小，覆盖即丢失全市场历史）。分流后 daily=全市场、daily_active=活跃池，
    回测按需 lake= 路由（LakeDataFetcher 的 dynamic_top50 走 daily_active）。

    容错：pool 空 / 板块流空 / 日线拉取失败 → 跳过对应落盘，绝不崩整个 sync。
    """
    out_sector = out_sector or LAKE_CONFIG["lakes"]["sector"]
    out_daily = out_daily or LAKE_CONFIG["lakes"]["daily_active"]
    if top_n is None:
        top_n = AKSHARE_CONFIG.get("top_sectors", 3)
    if pool_size is None:
        pool_size = AKSHARE_CONFIG.get("active_pool_size", 50)

    client = AKShareClient()
    pool = select_active_pool(client, top_n=top_n, pool_size=pool_size)
    if not pool:
        print("活跃池为空，跳过日线落盘")
        # 活跃池空仍尝试落板块资金流（板块层与个股层解耦，互不阻塞）
    else:
        print(f"活跃池 {len(pool)} 只：{pool[:5]}{'...' if len(pool) > 5 else ''}")

    # ---- 板块资金流落盘（板块层独立于个股池）----
    flow = client.fetch_sector_fund_flow()
    if flow is not None and not flow.empty:
        os.makedirs(os.path.dirname(out_sector), exist_ok=True)
        flow.to_parquet(out_sector)
        print(f"板块资金流落盘：{out_sector}（{len(flow)} 行）")
    else:
        print("板块资金流为空，跳过 sector 落盘")

    if not pool:
        return

    # ---- 活跃池近 1 年前复权日线合并落盘 ----
    end = _today()
    start = _shift(365)
    pieces: list[pd.DataFrame] = []
    for sym in pool:
        df = client.fetch_daily_hist(sym, start, end, adjust="qfq")
        if df is None or df.empty:
            continue
        df = df.copy()
        df["symbol"] = sym
        # _cleanse_daily 已把 date 设为 DatetimeIndex；reset_index 取出 date 列
        df = df.reset_index().rename(columns={"index": "date"})
        if "date" not in df.columns and "日期" in df.columns:
            df = df.rename(columns={"日期": "date"})
        pieces.append(df)
    if pieces:
        big = pd.concat(pieces, ignore_index=True)
        big["date"] = pd.to_datetime(big["date"])
        big = big.set_index(["date", "symbol"]).sort_index()
        os.makedirs(os.path.dirname(out_daily), exist_ok=True)
        big.to_parquet(out_daily)
        print(f"活跃池日线落盘：{out_daily}（{len(big)} 行）")
    else:
        print("活跃池日线全空（可能全停牌或限频），跳过 daily 落盘")


def _today() -> str:
    """今日 'YYYY-MM-DD'。"""
    return _dt.date.today().strftime("%Y-%m-%d")


def _shift(days: int) -> str:
    """今日往前推 `days` 天，'YYYY-MM-DD'。"""
    return (_dt.date.today() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    sync_sector_daily()
