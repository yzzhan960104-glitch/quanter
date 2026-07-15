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
import time
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from config import TUSHARE_DATASETS, LAKE_CONFIG
from data._tushare_compat import get_pro, source_name
from data.resilience import tushare_breaker, tushare_rate_limiter

logger = logging.getLogger(__name__)


# ============ 限频退避参数 ============
# Why 集中常量：瞬时态限频的退避策略需可调（全量下载时撞 tnskhdata 代理服务端限频，
# 需要更长退避），抽到模块顶部便于排查 + 测试时 monkeypatch。
# base=2s + max_retries=5 → 序列 2/4/8/16/32s（最坏单接口 ~62s）。
# Why 上限 32s：退避不能无限烧积分，最坏 ~1 分钟退避后仍失败则 record_failure 走熔断。
_BACKOFF_BASE_SEC = 2.0
_BACKOFF_MAX_RETRIES = 5

# 熔断 OPEN 时的冷却等待：sleep 一个 recovery_timeout 让 breaker 自动 HALF_OPEN，
# 再试 1 次（避免 by=date 全历史一旦熔断整数据集永远拉不到，卡片全空）。
# Why 用 breaker.recovery_timeout 而非硬编码：保持熔断参数单一事实源。
_BREAKER_OPEN_WAIT_RETRIES = 1


def _classify_exc(e: Exception) -> str:
    """异常分类：transient（瞬时态/限频/超时/断网）/ persistent（持久态/积分权限）/ unknown。

    Why 显式分类而非在 _fetch_with_guard 里散落 if-elif：
      - 三态处理策略差异大（瞬时态退避重试、持久态直接返空、未知态保守熔断），
        抽出纯函数便于单测逐态覆盖（mock 抛特定异常 → 验证对应行为）；
      - 关键词集中一处，避免限频关键词（limit/频率/429/timeout...）在多处重复维护漂移。
    """
    msg = str(e).lower()
    # 瞬时态：服务端限频 / 网络抖动 / 连接重置 / 代理繁忙 —— 退避后可能成功
    if any(k in msg for k in (
        "limit", "429", "timeout", "connection", "频率", "超时", "频繁",
        "busy", "rate", "retry", "reset", "broken pipe", "timed out",
    )):
        return "transient"
    # 持久态：积分/权限不足 —— 重试必败，与外部接口健康无关
    if any(k in msg for k in ("积分", "权限", "permission", "forbidden", "403")):
        return "persistent"
    return "unknown"


def _fetch_with_guard(api_name: str, **kwargs) -> pd.DataFrame:
    """限频 + 熔断 + 异常分类包装的 pro 接口调用，空数据/失败返空 DF。

    三态处理（关键修复：瞬时态限频改指数退避重试，而非原直接 record_failure 返空）：
      - transient（限频/超时/断网）：指数退避重试（2/4/8/16/32s），重试期间**不**
        record_failure（限频是瞬时态，退避后可能成功，不应污染熔断计数拖累其他接口）；
        max_retries 全失败才 record_failure 一次 + 返空。
      - persistent（积分/权限）：不重试直接返空 + 日志，不 record_failure（重试必败
        且与接口健康无关，计熔断会误 OPEN 拖累正常接口）。
      - unknown：保守 record_failure 一次 + 返空（宁可误 OPEN 也不漏防线）。

    Why 熔断 OPEN 不直接返空（原 bug）：by=date 全市场逐日拉时连续限频 → breaker OPEN
    → 原 allow_request False 直接返空 → 整数据集永远拉不到（全空）。改为 sleep 一个
    recovery_timeout 让 breaker 自动 HALF_OPEN 再试，给大数据集一条活路。

    空数据（df.empty）是「正常无数据」语义，直接返空不计熔断（不 record_failure，
    也不 record_success —— 空数据不污染熔断计数，原逻辑保持）。
    """
    pro = get_pro()
    # 限频令牌桶：阻塞至令牌可用（桶容量+匀速补充已由 RateLimiter 管控，此处只扣 1）
    tushare_rate_limiter.acquire(1.0)

    # 熔断 OPEN 冷却重试：allow_request False 时不直接返空，sleep recovery_timeout
    # 让 breaker 自动转 HALF_OPEN，再走一次完整重试链（最多 _BREAKER_OPEN_WAIT_RETRIES 次）。
    # Why 不在 OPEN 时直接调用 pro：HALF_OPEN 名额限制会拒绝，必须等冷却到期。
    breaker_waits = 0
    while not tushare_breaker.allow_request():
        if breaker_waits >= _BREAKER_OPEN_WAIT_RETRIES:
            logger.warning("Tushare %s 熔断 OPEN，冷却重试 %d 次后仍不放行，返空",
                           api_name, breaker_waits)
            return pd.DataFrame()
        wait = tushare_breaker.recovery_timeout
        logger.warning("Tushare %s 熔断 OPEN，sleep %.0fs 等 HALF_OPEN 后重试 (wait %d/%d)",
                       api_name, wait, breaker_waits + 1, _BREAKER_OPEN_WAIT_RETRIES)
        time.sleep(wait)
        breaker_waits += 1

    # 瞬时态限频指数退避重试：2/4/8/16/32s，最多 _BACKOFF_MAX_RETRIES 次
    # Why 退避而非立即失败：tnskhdata 代理对大数据接口（by=date 全市场逐日）有服务端
    # 限频，瞬时撞限频后退避几秒通常即可恢复；原实现直接 record_failure 返空导致
    # by=date 全历史一旦撞限频就卡死（连续 record_failure → breaker OPEN → 全空）。
    last_exc: Exception | None = None
    for attempt in range(_BACKOFF_MAX_RETRIES + 1):  # 0..max_retries，首次不退避
        try:
            df = getattr(pro, api_name)(**kwargs)
        except Exception as e:
            kind = _classify_exc(e)
            # 持久态：积分/权限不足，重试必败，直接返空（不 record_failure，原逻辑）
            if kind == "persistent":
                logger.error("Tushare %s 积分/权限不足（持久态，不重试）：%s", api_name, e)
                return pd.DataFrame()
            # 未知态：保守 record_failure 一次 + 返空（宁可误 OPEN 也不漏防线，原逻辑）
            if kind == "unknown":
                tushare_breaker.record_failure()
                logger.error("Tushare %s 拉取失败（未知异常，保守熔断）：%s", api_name, e)
                return pd.DataFrame()
            # transient：瞬时态限频 —— 退避重试，重试期间不污染熔断计数
            last_exc = e
            if attempt >= _BACKOFF_MAX_RETRIES:
                break  # 退避次数耗尽，跳出走最终 record_failure
            backoff = _BACKOFF_BASE_SEC * (2 ** attempt)  # 2,4,8,16,32s
            logger.warning("Tushare %s 瞬时态限频 (retry %d/%d)，sleep %.0fs 后重试：%s",
                           api_name, attempt + 1, _BACKOFF_MAX_RETRIES, backoff, e)
            time.sleep(backoff)
            continue
        # 成功路径
        if df is None or df.empty:
            # 正常无数据：record_success 维持熔断器健康度（空数据 ≠ 接口异常）
            return pd.DataFrame()
        tushare_breaker.record_success()
        return df

    # 退避耗尽仍失败：此时才 record_failure（瞬时态持续不恢复 → 视为接口异常走熔断）
    tushare_breaker.record_failure()
    logger.error("Tushare %s 瞬时态限频退避 %d 次仍失败，record_failure 返空：%s",
                 api_name, _BACKOFF_MAX_RETRIES, last_exc)
    return pd.DataFrame()


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
    # 不可用数据集跳过（B 类·方法名错订正）：tnskhdata 代理对部分接口无支持
    # （如 concept），配置层标 _unavailable 后此处检测跳过，不下载/不报错，打印提示。
    if cfg.get("_unavailable"):
        logger.warning("⚠️ %s 跳过同步（不可用）：%s", key, cfg["_unavailable"])
        return
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
        # single 模式传 date_col + cfg + start/end：Plan C 宏观湖（cn_cpi/ppi/gdp/pmi/shibor）
        # 依赖 cfg['index_mode']=='datetime' 把月/季/日频列重建为 DatetimeIndex；
        # shibor/shibor_quote 等需按区间拉取的接口通过 cfg['date_range']=True 让
        # _sync_single 把 start/end 转 start_date/end_date 传给 API（避免全历史/近段）。
        _sync_single(key, api, fields, date_col, out, cfg=cfg, start=start, end=end)
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
        # 按 cfg['universe'] 自动路由标的池（stock/etf/index），不再硬调 _load_universe。
        # Why：ETF/指数类若喂股票列表会静默落空（接口返空→df.empty→continue，湖缺数据无感知）。
        symbols = resolve_symbols(key)
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
    # code_param：逐标的拉取时传给 API 的参数名（缺省 ts_code）。
    # Why 显式配置：多数 Tushare 接口按 ts_code 拉标的，但部分接口参数名不同（如
    # index_weight 用 index_code 拉指数成分权重）。缺省 ts_code 兼容既有数据集，仅
    # 需改参数名的数据集在配置层声明 code_param，零改框架分支。
    code_param = (cfg or {}).get("code_param", "ts_code")
    for ts_code in symbols:
        shard = os.path.join(shard_dir, f"{ts_code}.parquet")
        if resume and os.path.exists(shard):
            continue
        kwargs = {code_param: ts_code, "start_date": sd, "end_date": ed}
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


def _sync_single(key, api, fields, date_col, out, cfg=None, start=None, end=None):
    """单次拉取（指数/列表/宏观）。index_mode=datetime 时落 DatetimeIndex。

    Why 不分页：指数日线/概念字典/宏观指标等接口支持一次性按区间拉全量（或本身无
    区间概念），无需 shard/断点续传的复杂度，直接落盘。

    Why 三个区间/参数增强（quick 批暴露的 5 数据集 sync bug 修复）：
      - cfg['params']（dict）：额外 API 参数，合并进 kwargs。fund_basic 需传
        market='E' 过滤场内基金（实测 market='EFT' 返 0 行——EFT 是错误码，E 才是
        Tushare 场内基金真实 market 值）。_sync_single 原只传 fields，无法传这类
        必需参数，导致 fund_basic 拉到全量 15000 行（含 13827 场外基金）污染标的池。
      - cfg['date_range']=True + start/end：把区间转 start_date/end_date（YYYYMMDD）
        合并进 kwargs。shibor 无参返最近 2000 行（2018 起，全历史分页上限），加区间
        后精确返近 3 年 249 行，避免烧配额拉无用历史 + 落盘膨胀。
      - start/end 签名：由 sync_dataset 传入（与 by=symbol/date 共享区间语义），single
        模式下仅在 date_range=True 时消费，其余 single 数据集（concept/margin_secs 等
        静态快照）不传区间保持原行为。

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
    cfg = cfg or TUSHARE_DATASETS[key]
    kwargs = {}
    if fields:
        kwargs["fields"] = fields
    # cfg['params'] 合并：fund_basic 的 market='E' 等必需 API 参数（实测 market='EFT'=0 行，
    # E 才是场内基金真实码）。浅拷贝避免 mutate 全局配置（params 是共享 dict 引用）。
    extra_params = (cfg or {}).get("params")
    if extra_params:
        kwargs.update(dict(extra_params))
    # cfg['date_range']=True：把 start/end 转 start_date/end_date（YYYYMMDD）合并进 kwargs。
    # Why shibor 等接口无参返最近 2000 行（2018 起全历史），加区间精确返近 3 年，省配额
    # + 避免落盘膨胀。仅 date_range=True 时消费 start/end，其余 single 数据集不传区间。
    if (cfg or {}).get("date_range") and start and end:
        kwargs["start_date"] = start.replace("-", "")
        kwargs["end_date"] = end.replace("-", "")
    df = _fetch_with_guard(api, **kwargs)
    if df.empty:
        logger.warning("%s 数据为空，跳过", key)
        return
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
    # fields 筛选（落湖前）：部分接口（如 cn_pmi 宏观）忽略 fields 参数，请求 fields='A,B'
    # 仍返回全部列（含 UPDATE_BY/CREATE_BY 等 100% NaN 垃圾元字段）。此处按 cfg.fields 只保留
    # 声明列，防湖膨胀 + 防垃圾列污染下游。Why 放 index 建立后：date_col 已转 index 不在
    # df.columns，fields 含 date_col 也不影响（只筛 columns，index 始终保留）；对尊重 fields 的
    # 接口（落湖列本就 ⊆ fields）keep=全部 columns，筛选无变化，中性安全。
    if fields:
        fields_set = {f.strip() for f in fields.split(",")}
        keep = [c for c in df.columns if c in fields_set]
        if keep:
            df = df[keep]
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
    """ETF 标的列表（fund_basic market='E'，场内基金）。

    Why 独立于 _load_universe：_load_universe 拉 stock_basic（A 股股票，剔 ST/退），
    而 ETF 走 fund_basic market='E' 接口，数据源与过滤口径完全不同。ETF 不适用
    ST/退市过滤（基金无 ST 概念），故单独 helper，由调用方按 --market etf 显式选用。

    Why market='E' 服务端过滤（事实订正，quick 批暴露）：fund_basic 同时含场内基金（E）
    与场外基金（O，13827 只）。实测 market='EFT' 返 **0 行**（EFT 不是 Tushare 真实
    market 码——属易混事实，曾误以为内部码是 EFT），market='E' 才返场内基金。在服务端
    用 market='E' 过滤比客户端过滤省回传带宽 + 配额。返回 ts_code 列表作为
    fund_daily/fund_nav/fund_portfolio/fund_share 的 by=symbol 标的池。

    注意 market='E' 实测返 2864 行（tnskhdata 代理口径），含 ETF+LOF+REITs+封闭式等
    全部场内基金，标的池略宽于纯 ETF（~1173），但 fund_daily 对非 ETF 场内基金也返
    日线行情，不致命；下游若需纯 ETF 可再按 fund_type 过滤。
    """
    df = _fetch_with_guard("fund_basic", market="E",
                           fields="ts_code,name,market,management,found_date,list_date")
    if df.empty:
        return []
    # 双保险：服务端已按 market=E 过滤，客户端再兜一层（防接口行为漂移导致场外基金混入）
    if "market" in df.columns:
        df = df[df["market"] == "E"]
    return df["ts_code"].tolist()


# A 股核心宽基指数（覆盖主流规模/板块风格）。指数类数据集（index_daily/index_member）
# 的标的池——固定常量，无需也不应从股票/基金接口拉（与 _load_universe 股票池语义隔离）。
CORE_INDEX_CODES: list[str] = [
    "000300.SH",  # 沪深300
    "000905.SH",  # 中证500
    "000852.SH",  # 中证1000
    "000016.SH",  # 上证50
    "000688.SH",  # 科创50
    "399006.SZ",  # 创业板指
    "399001.SZ",  # 深证成指
    "000001.SH",  # 上证指数
]


def resolve_symbols(key: str, limit: Optional[int] = None) -> list[str]:
    """按 cfg['universe'] 自动选 by=symbol 数据集的标的池。

    Why 存在：by=symbol 数据集标的来源有三类——股票（财报/股东/筹码）、ETF（fund_*）、
    指数（index_*）。旧逻辑（_sync_by_symbol 在 symbols=None 时硬调 _load_universe）把三类
    统一喂股票列表，导致 fund_*/index_* 被喂错标的 → 接口返空 → 静默落空（df.empty 直接
    continue，不报错不落盘，湖里缺数据却无感知）。本函数按数据集声明的 universe 字段路由
    到正确 loader，让单数据集 CLI 与编排脚本零特殊分支都自动正确。

    参数：
        key: TUSHARE_DATASETS 注册的数据集 key
        limit: 切前 N 个标的（编排脚本子集验证用，如先跑沪深300 子集）
    """
    cfg = TUSHARE_DATASETS[key]
    universe = cfg.get("universe", "stock")  # 缺省 stock（向后兼容未显式声明的数据集）
    if universe == "etf":
        syms = _load_etf_universe()
    elif universe == "index":
        syms = list(CORE_INDEX_CODES)
    else:  # stock（含缺省）
        syms = _load_universe()
    if limit:
        syms = syms[:limit]
    return syms


def _trade_days(start: str, end: str) -> list[str]:
    """交易日历（trade_cal is_open=1）。返回 ['YYYYMMDD', ...]。

    Why 经 trade_cal 而非 pd.bdate_range：A 股节假日（春节/国庆等）非标准周末规则，
    必须用交易所官方日历，否则会对节假日发空请求浪费配额 + 误判为接口异常。
    """
    df = _fetch_with_guard("trade_cal", exchange="SSE",
                           start_date=start.replace("-", ""),
                           end_date=end.replace("-", ""), is_open="1")
    return df["cal_date"].tolist() if not df.empty else []
