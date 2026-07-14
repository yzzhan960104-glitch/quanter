# -*- coding: utf-8 -*-
"""通用 Tushare 湖同步器：配置驱动，一个框架覆盖所有时序接口。

各数据集在 config.TUSHARE_DATASETS 声明接口/字段/分页模式/落湖，本模块统一执行：
_fetch_with_guard 限频+熔断 → 分页拉取 → shard 断点续传 → build_multiindex 落湖。

分页模式（by）：
  - symbol：逐标的拉取（财报/股东等，单标的全历史一次返）
  - date：逐交易日拉取（资金流/龙虎榜/融资融券，单日全市场）
  - single：单次拉取（指数/列表类，不分页）

前视红线：财报类 date_col=ann_date（公告日），绝不用 end_date（报告期）。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from config import TUSHARE_DATASETS, LAKE_CONFIG
from data._tushare_compat import get_pro, source_name
from data.resilience import tushare_breaker, tushare_rate_limiter

logger = logging.getLogger(__name__)


def _fetch_with_guard(api_name: str, **kwargs) -> pd.DataFrame:
    """限频 + 熔断 + 异常分类包装的 pro 接口调用，空数据/失败返空 DF。

    瞬时态（限频/超时）计熔断；持久态（积分/权限）仅记日志；空数据不计熔断。
    与 sync_data_lake._fetch_with_guard 同范式。

    Why 异常分类而非一刀切 record_failure：
      - 限频/超时/断网属「瞬时态」，重试可能成功，应计入熔断阈值防连环打满被封；
      - 积分/权限不足属「持久态」，重试必败且与外部接口健康无关，计熔断会误OPEN
        拖累其他正常接口，故仅记日志、不record_failure；
      - 未知异常保守计熔断（宁可误OPEN也不漏防线）。
    空数据（df.empty）是「正常无数据」语义，调 record_success 维持熔断器健康度。
    """
    tushare_rate_limiter.acquire(1.0)
    if not tushare_breaker.allow_request():
        return pd.DataFrame()
    pro = get_pro()
    try:
        df = getattr(pro, api_name)(**kwargs)
    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ("limit", "429", "timeout", "connection", "频率", "超时", "频繁")):
            tushare_breaker.record_failure()
            logger.error("Tushare %s 限频/网络异常：%s", api_name, e)
        elif any(k in str(e) for k in ("积分", "权限")):
            logger.error("Tushare %s 积分不足：%s", api_name, e)
        else:
            tushare_breaker.record_failure()
            logger.error("Tushare %s 拉取失败：%s", api_name, e)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    tushare_breaker.record_success()
    return df


def _shard_dir(key: str) -> str:
    """数据集 shard 目录（断点续传）。

    Why 配置可覆盖：默认 data_lake/shards/<key> 已足够；但测试/特殊场景需自定义
    （如 tmp_path 隔离），故尊重 cfg["shard_dir"] 优先。
    """
    cfg = TUSHARE_DATASETS[key]
    return cfg.get("shard_dir", os.path.join("data_lake", "shards", key))


def _build_multiindex(shard_dir: str, date_col: str, symbol_col: str, out: str,
                      by: str = "symbol") -> None:
    """合并 shard → MultiIndex(date, symbol) parquet。

    Why MultiIndex(date, symbol)：列式 parquet 上 (date, symbol) 双层索引支持
    DataLakeReader 按日期区间 + 标的列表双向切片，避免单层索引二次过滤的内存膨胀。

    Why by 参数区分 symbol 来源（关键反前视偏差防线）：
      - by=symbol：shard 是「单标的全历史」（如 000001.SZ.parquet），shard 内无
        symbol 列，symbol 必须从文件名取（f.replace('.parquet',''))；
      - by=date：shard 是「单日全市场」（如 20240103.parquet），shard 内已含
        symbol_col 列（多标的），symbol 必须从该列取。若误用文件名会把交易日
        串（'20240103'）当成 symbol 落湖，symbol 级全错——这是前视偏差之外的
        数据污染，必须在落湖层堵死。

    兼容点：shard 文件可能由 _cleanse 写入（已把 date_col 转为名为 date_col 的
    DatetimeIndex），也可能由外部预置（索引名各异）。此处统一 reset_index 后
    按 date_col 重命名为 date，再 to_datetime 兜底非法/空值。
    """
    frames = []
    for f in os.listdir(shard_dir):
        if not f.endswith(".parquet"):
            continue
        df = pd.read_parquet(os.path.join(shard_dir, f))
        if by == "symbol":
            # 单标的全历史 shard：symbol 来自文件名
            df["symbol"] = f.replace(".parquet", "")
        # by=date：symbol 已在 symbol_col 列中，无需额外赋值
        df = df.reset_index().rename(columns={date_col: "date"})
        if "date" not in df.columns:
            df = df.rename(columns={df.columns[0]: "date"})
        frames.append(df)
    if not frames:
        raise RuntimeError(f"shard 目录无数据：{shard_dir}")
    big = pd.concat(frames, ignore_index=True)
    big["date"] = pd.to_datetime(big["date"])
    if by == "date":
        # by=date：symbol_col 列重命名为 symbol（统一索引名），保留真实标的码。
        # 边界（市场级时序，如 szse_daily/sse_daily：date_col==symbol_col==trade_date，
        # 无独立 symbol 列）：此时 trade_date 已在上一步被改名为 date，无法再 rename，
        # 故用 date 列的字符串副本作 symbol 层（symbol 恒等于交易日，冗余但符合 by=date
        # 的 MultiIndex(date, symbol) 契约，DataLakeReader 按 date 单级切片即可）。
        if symbol_col != date_col and symbol_col in big.columns:
            big = big.rename(columns={symbol_col: "symbol"})
        else:
            # 市场级时序：symbol 层 = 交易日字符串（与 date 同源，但作为第二级索引独立存在）
            big["symbol"] = big["date"].dt.strftime("%Y%m%d")
    big = big.set_index(["date", "symbol"]).sort_index()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    big.to_parquet(out, engine="pyarrow")
    logger.info("湖写入完成：%s，%d 行，%d 标的",
                out, len(big), big.index.get_level_values("symbol").nunique())


def sync_dataset(key: str, start: str, end: str,
                 symbols: Optional[list[str]] = None,
                 resume: bool = True) -> None:
    """按 TUSHARE_DATASETS[key] 配置同步一个数据集。

    参数：
        key: 数据集 key（TUSHARE_DATASETS 注册）
        start/end: "YYYY-MM-DD" 区间
        symbols: by=symbol 时的标的列表（None=全市场，需上游 load_universe）
        resume: 断点续传（shard 已存在跳过）

    Why 入口收敛到一个函数：所有数据集走同一限频/熔断/落湖管道，调用方只需
    传 key + 区间，分页细节由 cfg["by"] 决定。新增数据集零新增分支代码。
    """
    cfg = TUSHARE_DATASETS[key]
    api = cfg["api"]
    by = cfg["by"]
    date_col = cfg["date_col"]
    symbol_col = cfg.get("symbol_col", "ts_code")
    fields = cfg.get("fields")
    out = cfg["lake"]

    if by == "symbol":
        _sync_by_symbol(key, api, fields, date_col, symbol_col, start, end, symbols, resume, out, cfg=cfg)
    elif by == "date":
        _sync_by_date(key, api, fields, date_col, symbol_col, start, end, resume, out, cfg=cfg)
    elif by == "single":
        # single 模式传 date_col + cfg：Plan C 宏观湖（cn_cpi/ppi/gdp/pmi/shibor）
        # 依赖 cfg['index_mode']=='datetime' 把月/季/日频列重建为 DatetimeIndex。
        _sync_single(key, api, fields, date_col, out, cfg=cfg)
    else:
        raise ValueError(f"未知分页模式 by={by}（key={key}）")


def _sync_by_symbol(key, api, fields, date_col, symbol_col, start, end,
                    symbols, resume, out, cfg=None):
    """逐标的拉取（财报/股东）。shard 按 symbol。

    Why 按 symbol 分片：财报类接口单标的全历史一次返回（无分页），按标的分片天然
    支持断点续传（某标的已拉过即跳过，省配额）+ 并行扩展（未来可按 symbol 分发 worker）。

    Why cfg['rename'] 应用在 _cleanse 后、落 shard 前：fund_daily 接口返 vol 列
    （与股票日线 volume 列名分叉），配置 rename={'vol':'volume'} 在落 shard 前归一，
    确保 etf_daily 湖与 a_shares_daily 湖列名一致，跨湖因子计算免分支。rename 只在
    shard 写入前做一次，后续 _build_multiindex 读取 shard 即拿到已归一列名。
    """
    if symbols is None:
        symbols = _load_universe()
    shard_dir = _shard_dir(key)
    os.makedirs(shard_dir, exist_ok=True)
    sd, ed = start.replace("-", ""), end.replace("-", "")
    rename = (cfg or {}).get("rename")
    # 特色数据通道标注（Plan A Task 9）：cyq_perf 等特色数据按 300/分独立计频，
    # 限流仍走统一 tushare_rate_limiter（refill_rate=1 token/s + 突发桶 capacity=5，
    # 持续 ~60/分，远严于特色数据 300/分配额，不会触发 Tushare 端限频），此处仅
    # 日志层标记 quota_type=special，便于限频排查时快速定位特色通道。放循环外只标一次，
    # 避免 5000+ 标的逐个打日志。debug 级别默认不输出，仅排查时开启。
    if (cfg or {}).get("quota_type") == "special":
        logger.debug("%s 为特色数据（300/分独立通道，限流仍走统一 rate_limiter）", key)
    for ts_code in symbols:
        shard = os.path.join(shard_dir, f"{ts_code}.parquet")
        if resume and os.path.exists(shard):
            continue
        kwargs = {"ts_code": ts_code, "start_date": sd, "end_date": ed}
        if fields:
            kwargs["fields"] = fields
        df = _fetch_with_guard(api, **kwargs)
        if df.empty:
            continue
        df = _cleanse(df, date_col)
        if rename:
            df = df.rename(columns=rename)  # 列名归一（如 fund_daily vol→volume）
        df.to_parquet(shard)
    _build_multiindex(shard_dir, date_col, symbol_col, out, by="symbol")


def _sync_by_date(key, api, fields, date_col, symbol_col, start, end, resume, out, cfg=None):
    """逐交易日拉取（资金流/龙虎榜/融资融券）。shard 按 date。

    Why 按日分片：此类接口单日全市场一次返回（无标的维度的全历史），按交易日分片
    支持断点续传（某日已拉即跳过）+ 增量同步友好（每日仅补最新一天）。

    Why 同样支持 cfg['rename']：与 _sync_by_symbol 对称（未来 by=date 接口可能也有
    列名归一需求，一次性补齐，避免日后反复改框架）。
    """
    shard_dir = _shard_dir(key)
    os.makedirs(shard_dir, exist_ok=True)
    trade_dates = _trade_days(start, end)
    rename = (cfg or {}).get("rename")
    for td in trade_dates:
        shard = os.path.join(shard_dir, f"{td}.parquet")
        if resume and os.path.exists(shard):
            continue
        kwargs = {"trade_date": td}
        if fields:
            kwargs["fields"] = fields
        df = _fetch_with_guard(api, **kwargs)
        if df.empty:
            continue
        df = _cleanse(df, date_col)
        if rename:
            df = df.rename(columns=rename)  # 列名归一（与 by=symbol 对称）
        df.to_parquet(shard)
    _build_multiindex(shard_dir, date_col, symbol_col, out, by="date")


def _sync_single(key, api, fields, date_col, out, cfg=None):
    """单次拉取（指数/列表/宏观）。index_mode=datetime 时落 DatetimeIndex。

    Why 不分页：指数日线/概念字典/宏观指标等接口支持一次性按区间拉全量（或本身无
    区间概念），无需 shard/断点续传的复杂度，直接落盘。

    Why index_mode='datetime' 分支（Plan C 宏观湖）：CPI/PPI/GDP/PMI/Shibor 是单一
    时间序列，落 DatetimeIndex（无 symbol 层），区别于股票湖的 MultiIndex(date, symbol)。
    原 single 路径直接 to_parquet 落扁平 df（时间列作普通列），DataLakeReader 按
    日期切片会 KeyError。index_mode='datetime' 时把 date_col 转时间索引。

    Why 三段 format 推断（关键反格式假设，规避 Tushare 字段格式漂移）：
      - 季频（YYYYQ1，如 '2024Q1'）：含 'Q' → pd.PeriodIndex(freq='Q').to_timestamp()
        （pandas 原生 to_datetime 不认 'Q'，必须经 PeriodIndex 中转，季度首月首日为锚点）
      - 月频（YYYYMM，6 位数字，如 '202401'）→ format='%Y%m'（月初首日）
      - 日频（YYYYMMDD，8 位数字，如 '20240105'）→ format='%Y%m%d'（兜底）
    format 错配会静默产出 NaT（errors='coerce'）→ dropna 清空整表，故必须按数据形态
    分流，不能一刀切。此处用字符串形态判断而非硬编码 key→format 映射，规避字段漂移。
    无 index_mode 时保持原扁平落盘（concept/margin_secs 等静态字典/快照不重建索引）。
    """
    kwargs = {}
    if fields:
        kwargs["fields"] = fields
    df = _fetch_with_guard(api, **kwargs)
    if df.empty:
        logger.warning("%s 数据为空，跳过", key)
        return
    cfg = cfg or TUSHARE_DATASETS[key]
    if cfg.get("index_mode") == "datetime" and date_col and date_col in df.columns:
        col = df[date_col].astype(str)
        if col.str.contains("Q", na=False).any():
            # 季频（YYYYQ1）→ PeriodIndex(freq='Q') → 季度首月首日 Timestamp
            # 例 2024Q1 → 2024-01-01，2024Q2 → 2024-04-01
            df[date_col] = pd.PeriodIndex(col, freq="Q").to_timestamp()
        elif col.str.len().eq(6).all():
            # 月频（YYYYMM，6 位）→ 月初首日
            df[date_col] = pd.to_datetime(col, format="%Y%m", errors="coerce")
        else:
            # 日频（YYYYMMDD，8 位）兜底
            df[date_col] = pd.to_datetime(col, format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=[date_col]).set_index(date_col).sort_index()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    df.to_parquet(out, engine="pyarrow")
    logger.info("%s 写入：%s，%d 行", key, out, len(df))


def _cleanse(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    """洗净：date_col → datetime 索引 + 升序。

    Why errors="coerce"：Tushare 偶发返回脏日期（空串/非标准格式），coerce 转 NaT
    后 dropna 剔除，避免 NaT 落入索引破坏 MultiIndex 排序与时序查询语义。
    format="%Y%m%d" 锁定 Tushare 日期格式（YYYYMMDD 数字串），避免 dateutil 慢解析。
    """
    if date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col], format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=[date_col]).set_index(date_col).sort_index()
    return df


def _load_universe() -> list[str]:
    """全市场在售标的（复用 sync_data_lake.load_universe 逻辑：stock_basic 剔 ST/退）。

    Why 剔 ST/退：ST/*ST/退市标的流动性差、财务异常，财报/资金流类数据集纳入会污染
    统计口径（如行业均值/中位数），故默认剔除。调用方若需全量可显式传 symbols。
    """
    df = _fetch_with_guard("stock_basic", list_status="L",
                           fields="ts_code,symbol,name,list_date")
    if df.empty:
        return []
    mask = (~df["name"].str.contains("ST", na=False)) & \
           (~df["name"].str.contains("退", na=False))
    return df.loc[mask, "ts_code"].tolist()


def _load_etf_universe() -> list[str]:
    """ETF 标的列表（fund_basic market='EFT'，仅场内 ETF）。

    Why 独立于 _load_universe：_load_universe 拉 stock_basic（A 股股票，剔 ST/退），
    而 ETF 走 fund_basic market='EFT' 接口，数据源与过滤口径完全不同。ETF 不适用
    ST/退市过滤（基金无 ST 概念），故单独 helper，由调用方按 --market etf 显式选用。

    Why market='EFT' 服务端过滤：fund_basic 同时含场内 ETF（EFT）与场外基金（OF），
    在服务端用 market='EFT' 过滤比客户端过滤省回传带宽 + 配额。返回 ts_code 列表
    作为 fund_daily/fund_nav/fund_portfolio/fund_share 的 by=symbol 标的池。

    注意 Tushare 内部码是 'EFT'（Exchange Fund Trader）而非 'ETF'，属易混事实。
    """
    df = _fetch_with_guard("fund_basic", market="EFT",
                           fields="ts_code,name,market,management,found_date,list_date")
    if df.empty:
        return []
    # 双保险：服务端已按 market=EFT 过滤，客户端再兜一层（防接口行为漂移导致场外基金混入）
    if "market" in df.columns:
        df = df[df["market"] == "EFT"]
    return df["ts_code"].tolist()


def _trade_days(start: str, end: str) -> list[str]:
    """交易日历（trade_cal is_open=1）。返回 ['YYYYMMDD', ...]。

    Why 经 trade_cal 而非 pd.bdate_range：A 股节假日（春节/国庆等）非标准周末规则，
    必须用交易所官方日历，否则会对节假日发空请求浪费配额 + 误判为接口异常。
    """
    df = _fetch_with_guard("trade_cal", exchange="SSE",
                           start_date=start.replace("-", ""),
                           end_date=end.replace("-", ""), is_open="1")
    return df["cal_date"].tolist() if not df.empty else []
