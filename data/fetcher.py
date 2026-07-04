"""数据获取统一接口

职责：
1. 定义抽象数据获取接口
2. 实现模拟数据获取器（用于开发与测试）
3. 实现真实数据源接入（FRED 宏观锚点 / Tushare A股基本面）
4. 组合模式聚合网关（双轨数据架构：慢车道分析 + 快车道量价）

设计原则：
- 第一性原理：返回纯 Pandas DataFrame，无黑盒封装
- 前视偏差防范：宏观数据返回发布时间，而非数据发生时间
- 异常值标记：不静默处理缺失值，而是显式标记

缓存策略：
- FRED/Tushare 的慢车道数据使用 Parquet 本地落盘缓存
- 相同 (source, key, start, end) 的第二次请求直接毫秒级读取
- 保护 API Token：减少限频风险，降低积分消耗
- 缓存文件路径：data/cache/{source}/{key}_{start}_{end}.parquet
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Dict, List
import hashlib
import logging
from pathlib import Path

import numpy as np
import pandas as pd

# 容灾基建（限流器令牌桶 + 手动熔断器 + 异常类型）
# 设计原则：fetcher 保留"任何异常都返回空 DataFrame、绝不抛"的对外契约，
# 仅在内部用 breaker 的手动 API（allow_request / record_success / record_failure）
# 加保护 —— OPEN 时快速返回空 DF，基础设施异常计入熔断，但不改变对外行为。
from data.resilience import (
    DataFetchError,
    fred_breaker,
    fred_rate_limiter,
    tushare_breaker,
    tushare_rate_limiter,
)

# 配置模块级日志
logger = logging.getLogger(__name__)

# ============ 缓存目录常量 ============
# 相对于项目根目录的缓存路径
_CACHE_BASE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"


def _cache_key(source: str, identifier: str, start: datetime, end: datetime) -> str:
    """
    生成缓存文件名（确定性哈希）

    参数：
        source: 数据源名称（如 "fred", "tushare"）
        identifier: 唯一标识（如指标代码 "DGS10" 或 "600000.SH_pe"）
        start: 请求起始时间
        end: 请求结束时间

    返回：
        缓存文件名（不含目录），格式：{identifier}_{start}_{end}.parquet
    """
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")
    # 清理标识符中的特殊字符（如 . /）
    safe_id = identifier.replace(".", "_").replace("/", "_")
    return f"{source}_{safe_id}_{start_str}_{end_str}.parquet"


def _read_parquet_cache(source: str, identifier: str,
                        start: datetime, end: datetime) -> Optional[pd.DataFrame]:
    """
    从本地 Parquet 缓存读取数据

    参数：
        source: 数据源名称
        identifier: 唯一标识
        start: 请求起始时间
        end: 请求结束时间

    返回：
        缓存的 DataFrame，缓存不存在时返回 None
    """
    cache_dir = _CACHE_BASE_DIR / source
    cache_file = cache_dir / _cache_key(source, identifier, start, end)

    if not cache_file.exists():
        return None

    try:
        df = pd.read_parquet(cache_file)
        # Parquet 存储时可能丢失 tz 信息，恢复 Asia/Shanghai
        if not df.empty and isinstance(df.index, pd.DatetimeIndex):
            if df.index.tz is None:
                df.index = df.index.tz_localize("Asia/Shanghai")
        logger.info(f"缓存命中：{cache_file.name}（跳过 API 请求）")
        return df
    except Exception as e:
        # 缓存文件损坏时不阻塞业务，删除坏缓存继续走 API
        logger.warning(f"缓存文件损坏，删除并回退到 API：{cache_file.name} - {e}")
        try:
            cache_file.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def _write_parquet_cache(source: str, identifier: str,
                         start: datetime, end: datetime,
                         df: pd.DataFrame) -> None:
    """
    将数据写入本地 Parquet 缓存

    参数：
        source: 数据源名称
        identifier: 唯一标识
        start: 请求起始时间
        end: 请求结束时间
        df: 待缓存的数据

    注意：
        - 空 DataFrame 不写入缓存（避免后续读取空数据跳过 API）
        - Parquet 格式不支持 tz-aware DatetimeIndex，
          写入前先 strip 时区，读取时恢复（见 _read_parquet_cache）
    """
    if df.empty:
        return  # 空数据不缓存

    cache_dir = _CACHE_BASE_DIR / source
    cache_file = cache_dir / _cache_key(source, identifier, start, end)

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Parquet 不支持 tz-aware DatetimeIndex，写入前去除时区
        df_to_save = df.copy()
        if isinstance(df_to_save.index, pd.DatetimeIndex) and df_to_save.index.tz is not None:
            df_to_save.index = df_to_save.index.tz_localize(None)

        df_to_save.to_parquet(cache_file, engine="pyarrow")
        logger.info(f"缓存写入：{cache_file.name}（{len(df)} 行）")
    except Exception as e:
        # 缓存写入失败不阻塞业务，仅记录警告
        logger.warning(f"缓存写入失败：{cache_file.name} - {e}")


class DataFetcher(ABC):
    """数据获取统一接口，支持多数据源切换"""

    @abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        freq: str = "1d"
    ) -> pd.DataFrame:
        """
        获取 OHLCV 数据

        参数：
            symbol: 交易标的代码（如 "600000.SH"）
            start: 起始时间
            end: 结束时间
            freq: 频率（"1d"/"1h"/"5m"/"1m"）

        返回：
            DataFrame with index: DatetimeIndex (tz-aware, Asia/Shanghai)
            columns: ['open', 'high', 'low', 'close', 'volume', 'amount']

        注意：
            - 必须返回 tz-aware 的时间戳
            - 涨跌停板日的价格应包含实际成交价（而非理论限价）
            - 缺失交易日不应被插值（前视偏差防范）
        """
        pass

    @abstractmethod
    def fetch_macro(
        self,
        indicator: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """
        获取宏观数据

        参数：
            indicator: 宏观指标名称（如 "m2", "cpi", "ppi"）
            start: 起始时间
            end: 结束时间

        返回：
            DataFrame with index: DatetimeIndex（发布时间，防范前视偏差）
            columns: [indicator]

        注意：
            - index 必须是发布时间，而非数据发生时间
            - 例如：2024年1月CPI可能在2024年2月15日发布
            - 信号只能在发布日及之后生效
        """
        pass

    @abstractmethod
    def fetch_factor_data(
        self,
        symbol: str,
        factor_name: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """
        获取因子数据（如 P/E、市净率）

        参数：
            symbol: 交易标的代码
            factor_name: 因子名称
            start: 起始时间
            end: 结束时间

        返回：
            DataFrame with index: DatetimeIndex
            columns: [factor_name]

        注意：
            - 基本面数据存在前视偏差风险（财报发布滞后）
            - 应返回数据的"可见日期"，而非"数据日期"
        """
        pass


class MockDataFetcher(DataFetcher):
    """
    Mock 数据获取器（用于开发与测试）

    生成符合 A 股特征的模拟数据：
    - 涨跌停板限制（10%/20%）
    - 成交量波动
    - 价格趋势（可配置）
    """

    def __init__(self, seed: Optional[int] = 42):
        """
        初始化 Mock 数据生成器

        参数：
            seed: 随机种子（确保可复现）
        """
        self.rng = np.random.default_rng(seed)

    def fetch_ohlcv(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        freq: str = "1d"
    ) -> pd.DataFrame:
        """生成模拟 OHLCV 数据"""
        # 生成日期范围（仅包含交易日，排除周末）
        date_range = pd.date_range(
            start=start,
            end=end,
            freq="B"  # Business days（排除周末）
        )
        date_range = date_range.tz_localize("Asia/Shanghai")

        # 模拟价格（几何布朗运动）
        n = len(date_range)
        returns = self.rng.normal(loc=0.0005, scale=0.02, size=n)
        prices = np.cumprod(1 + returns) * 100  # 起始价格 100 元

        # 模拟 OHLC（开盘价 = 前一日收盘价 ± 小幅随机）
        opens = np.roll(prices, 1)
        opens[0] = prices[0]
        opens += self.rng.normal(loc=0, scale=0.5, size=n)

        # 最高价和最低价（基于开盘价和收盘价）
        highs = np.maximum(opens, prices) + self.rng.uniform(0, 1, size=n)
        lows = np.minimum(opens, prices) - self.rng.uniform(0, 1, size=n)

        # 模拟成交量（对数正态分布）
        volumes = self.rng.lognormal(mean=15, sigma=0.5, size=n)

        # 模拟成交额（成交量 × 平均价）
        amounts = volumes * ((opens + prices) / 2)

        # 构建 DataFrame
        df = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": prices,
                "volume": volumes,
                "amount": amounts,
            },
            index=date_range
        )

        # 应用涨跌停板限制（10%）
        limit_up = 1.10
        limit_down = 0.90

        # 涨停处理
        limit_up_mask = df["close"] >= df["open"].shift(1) * limit_up
        df.loc[limit_up_mask, "close"] = df.loc[limit_up_mask, "open"].shift(1) * limit_up
        df.loc[limit_up_mask, "high"] = df.loc[limit_up_mask, "close"]
        df.loc[limit_up_mask, "low"] = df.loc[limit_up_mask, "open"]

        # 跌停处理
        limit_down_mask = df["close"] <= df["open"].shift(1) * limit_down
        df.loc[limit_down_mask, "close"] = df.loc[limit_down_mask, "open"].shift(1) * limit_down
        df.loc[limit_down_mask, "low"] = df.loc[limit_down_mask, "close"]
        df.loc[limit_down_mask, "high"] = df.loc[limit_down_mask, "open"]

        # 跌停日成交量萎缩
        df.loc[limit_down_mask, "volume"] *= 0.1

        return df

    def fetch_macro(
        self,
        indicator: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """生成模拟宏观数据"""
        # 宏观数据月度发布
        date_range = pd.date_range(
            start=start,
            end=end,
            freq="MS"  # Month start
        )
        date_range = date_range.tz_localize("Asia/Shanghai")

        # 模拟 M2 增速（正态分布，均值 8%）
        if indicator == "m2":
            values = self.rng.normal(loc=0.08, scale=0.02, size=len(date_range))
        # 模拟 CPI（正态分布，均值 2%）
        elif indicator == "cpi":
            values = self.rng.normal(loc=0.02, scale=0.01, size=len(date_range))
        else:
            values = self.rng.normal(loc=0.05, scale=0.05, size=len(date_range))

        df = pd.DataFrame(
            {indicator: values},
            index=date_range
        )

        # 模拟发布延迟（数据在下月 15 日发布，防范前视偏差）
        # 注意：在实际系统中，应从数据源获取真实的发布时间
        df.index = df.index + pd.DateOffset(days=15)

        return df

    def fetch_factor_data(
        self,
        symbol: str,
        factor_name: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """生成模拟因子数据"""
        date_range = pd.date_range(
            start=start,
            end=end,
            freq="B"
        )
        date_range = date_range.tz_localize("Asia/Shanghai")

        # 模拟 P/E 比率（对数正态分布）
        if factor_name == "pe":
            values = self.rng.lognormal(mean=2.5, sigma=0.5, size=len(date_range))
        # 模拟市净率
        elif factor_name == "pb":
            values = self.rng.lognormal(mean=1.0, sigma=0.3, size=len(date_range))
        else:
            values = self.rng.normal(loc=10.0, scale=5.0, size=len(date_range))

        df = pd.DataFrame(
            {factor_name: values},
            index=date_range
        )

        return df


# ============================================================
# 真实数据源接入：FRED（美联储经济数据）
# ============================================================

class FredDataFetcher(DataFetcher):
    """
    FRED 宏观锚点数据获取器

    职责：
    - 拉取美联储经济数据（如 DGS10 十年期国债收益率、CPI、联邦基金利率等）
    - 强制防前视偏差：日频数据 shift(1)，月频数据滞后至次月中旬

    风控红线：
    - FRED 数据标注的是"发生日"而非"发布日"。日频数据（如 DGS10）
      在美国东部时间 15:30 后更新，对 A 股而言相当于 T+1 才可见。
    - 月频数据（如 CPIAUCSL）通常在次月中旬发布，强制滞后 15 个自然日。
    - 网络超时与限频 (HTTP 429) 必须显式捕获，返回空 DataFrame 而非崩溃。
    """

    # FRED 指标元数据注册表：频率 → 发布滞后规则
    # "d" = 日频, "m" = 月频, "q" = 季频
    FRED_FREQUENCY_MAP: Dict[str, str] = {
        "DGS10": "d",    # 美国10年期国债收益率（日频）
        "DGS2": "d",     # 美国2年期国债收益率（日频）
        "DFF": "d",      # 联邦基金有效利率（日频）
        "CPIAUCSL": "m", # 美国CPI（月频，城市消费者，季调）
        "PAYEMS": "m",   # 非农就业人数（月频）
        "UNRATE": "m",   # 失业率（月频）
        "GDP": "q",      # 实际GDP（季频）
        "FEDFUNDS": "m", # 联邦基金利率（月频）
        "T10Y2Y": "d",   # 10Y-2Y收益率利差（日频）
    }

    def __init__(self, api_key: Optional[str] = None):
        """
        初始化 FRED 数据获取器

        参数：
            api_key: FRED API Key，为 None 时从 config 层自动加载

        异常：
            ValueError: API Key 缺失时抛出
        """
        if api_key is None:
            from config import get_credential
            api_key = get_credential("fred", "api_key")

        try:
            from fredapi import Fred
            self._fred = Fred(api_key=api_key)
            logger.info("FRED API 客户端初始化成功")
        except ImportError:
            raise ImportError(
                "fredapi 未安装。请执行：pip install fredapi"
            )
        except Exception as e:
            raise ConnectionError(
                f"FRED API 初始化失败：{e}。请检查 API Key 是否有效。"
            )

        # 缓存字典：避免对同一指标重复请求
        self._cache: Dict[str, pd.DataFrame] = {}

    def fetch_ohlcv(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        freq: str = "1d"
    ) -> pd.DataFrame:
        """
        FRED 不提供 OHLCV 数据，抛出明确的不支持异常

        为什么不返回空 DataFrame 而是抛异常？
        → 静默返回空数据可能导致下游策略在无感知的情况下运行于缺失数据，
          这比崩溃更危险。显式失败是量化系统的安全网。
        """
        raise NotImplementedError(
            "FRED 是宏观经济数据源，不提供 OHLCV 行情数据。"
            "请使用 QMT/MockDataFetcher 获取量价数据。"
        )

    def fetch_macro(
        self,
        indicator: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """
        从 FRED 拉取宏观经济指标，并强制施加发布滞后（防前视偏差）

        参数：
            indicator: FRED 指标代码（如 "DGS10", "CPIAUCSL"）
            start: 起始时间
            end: 结束时间

        返回：
            DataFrame with index: DatetimeIndex (tz-aware, Asia/Shanghai)
            columns: [indicator]

        前视偏差防范逻辑：
        ┌──────────┬────────────────────────────────────────────┐
        │ 频率     │ 滞后规则                                   │
        ├──────────┼────────────────────────────────────────────┤
        │ 日频 (d) │ shift(1) — 数据在 T+1 才对 A 股策略可见    │
        │ 月频 (m) │ 滞后 15 个自然日 — 模拟次月中旬发布节奏     │
        │ 季频 (q) │ 滞后 45 个自然日 — 模拟季后 1.5 个月发布    │
        └──────────┴────────────────────────────────────────────┘
        """
        # ── 本地 Parquet 缓存查询（必须最先做）──
        # FRED 数据更新频率低（日/月/季），缓存命中率极高。
        # 同一 (indicator, start, end) 的第二次请求直接毫秒级读取。
        # 【关键】限流 acquire 与熔断守卫必须放在缓存命中检查之后：
        # 否则缓存命中也会白白扣令牌，导致高缓存命中率的并发回测被无谓限流。
        cached = _read_parquet_cache("fred", indicator, start, end)
        if cached is not None:
            return cached

        # 【限流】仅当缓存未命中、即将调真实 API 时才取令牌 —— 防 FRED 429 限频
        fred_rate_limiter.acquire(1.0)
        # 【熔断守卫】OPEN 则快速返回空 DF（保留既有"不抛"契约）
        # 原因：FRED 限频冷却约 60s，OPEN 期间继续打只会加重 FRED 防封禁
        if not fred_breaker.allow_request():
            logger.warning(f"FRED 熔断开启，跳过宏观指标请求：{indicator}")
            return pd.DataFrame(
                columns=[indicator],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        try:
            # 拉取原始序列（fredapi 返回 pd.Series）—— 抽到独立方法便于异常分类
            series = self._fetch_series_from_api(indicator, start, end)
            # 仅当真实 API 返回非空序列时才计一次成功 → 复位熔断计数。
            # 空数据（指标无数据）属于中性事件：既不算成功也不算失败，
            # 否则"429 与空数据交替"会不断复位计数，熔断永远不跳闸。
            if series is not None and not series.empty:
                fred_breaker.record_success()
        except Exception as e:
            # 捕获网络超时、HTTP 429 限频、无效指标代码等所有异常
            error_msg = str(e)
            if "429" in error_msg or "rate limit" in error_msg.lower():
                logger.error(f"FRED API 限频：{indicator}，请降低请求频率")
                # 基础设施类异常 → 计入熔断（连续达阈值后 OPEN，自动停打）
                fred_breaker.record_failure()
            elif "timeout" in error_msg.lower() or "connection" in error_msg.lower():
                logger.error(f"FRED API 网络超时/断线：{indicator}")
                # 网络层异常同样属于基础设施 → 计熔断
                fred_breaker.record_failure()
            else:
                logger.error(f"FRED API 拉取失败 [{indicator}]：{error_msg}")
                # 非基础设施类（如无效指标代码、解析错误）→ 不计熔断
            # 保留既有契约：统一返回空 DataFrame，不抛
            return pd.DataFrame(
                columns=[indicator],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        # 转为 DataFrame
        df = series.to_frame(name=indicator)

        # FRED 返回的 index 通常是 tz-naive 的 datetime64[ns]
        if df.index.tz is not None:
            # 如果已经是 tz-aware（罕见），先转为 UTC 再转 Asia/Shanghai
            df.index = df.index.tz_convert("Asia/Shanghai")
        else:
            # FRED 数据的原始时间戳是美东时间 (US/Eastern) 的日期，
            # 但作为纯日期（无时分秒），我们直接本地化为 Asia/Shanghai
            df.index = df.index.tz_localize("Asia/Shanghai")

        # ── 前视偏差防范：强制发布滞后 ──
        freq_type = self.FRED_FREQUENCY_MAP.get(indicator, "m")  # 默认月频

        if freq_type == "d":
            # 日频数据：shift(1)
            # 原因：FRED 日频数据（如 DGS10）在美东时间 15:30 后更新，
            # 对应北京时间凌晨，A股策略最早在次日 (T+1) 才可使用该数据
            df.index = df.index + pd.Timedelta(days=1)
            logger.debug(f"FRED 日频指标 [{indicator}] 已施加 shift(1) 发布滞后")
        elif freq_type == "m":
            # 月频数据：滞后 15 个自然日
            # 原因：CPI、非农等月度指标通常在次月 10-15 日发布，
            # 保守估计滞后 15 天，确保策略不可能提前看到
            df.index = df.index + pd.Timedelta(days=15)
            logger.debug(f"FRED 月频指标 [{indicator}] 已施加 15 天发布滞后")
        elif freq_type == "q":
            # 季频数据：滞后 45 个自然日
            # 原因：GDP 等季度指标通常在季后约 1.5 个月发布
            df.index = df.index + pd.Timedelta(days=45)
            logger.debug(f"FRED 季频指标 [{indicator}] 已施加 45 天发布滞后")

        # 过滤到请求的时间范围（发布滞后后，部分数据可能超出范围）
        # 注意：这里按发布后的索引过滤，确保返回的都是在 [start, end] 间可见的数据
        mask = (df.index >= pd.Timestamp(start, tz="Asia/Shanghai")) & \
               (df.index <= pd.Timestamp(end, tz="Asia/Shanghai"))
        df = df.loc[mask]

        # 显式处理 NaN：FRED 日频数据在周末/假日为 NaN
        # 量化策略不应看到这些 NaN，但也不能静默删除（可能掩盖数据源异常）
        nan_count = df[indicator].isna().sum()
        if nan_count > 0:
            logger.warning(
                f"FRED 指标 [{indicator}] 含 {nan_count} 个 NaN "
                f"（可能为周末/假日无数据）。下游需显式 ffill 或丢弃。"
            )

        # 更新内存缓存
        self._cache[indicator] = df

        # ── 写入本地 Parquet 缓存 ──
        # 保护 API Token：相同参数的后续请求直接读磁盘
        _write_parquet_cache("fred", indicator, start, end, df)

        return df

    def _fetch_series_from_api(
        self, indicator: str, start: datetime, end: datetime
    ) -> pd.Series:
        """
        真正调用 FRED API 拉取原始序列（fredapi 返回 pd.Series）。

        抽离动机：把"触达外部接口"的代码与"缓存/前视偏差处理/异常分类"解耦，
        便于熔断器在外层统一 try/except 包裹后做异常分类与 record_failure。
        本方法只负责调用 + 返回原始 Series，不做任何加工。
        """
        return self._fred.get_series(
            series_id=indicator,
            observation_start=start,
            observation_end=end
        )

    def fetch_factor_data(
        self,
        symbol: str,
        factor_name: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """FRED 不提供个股因子数据"""
        raise NotImplementedError(
            "FRED 是宏观经济数据源，不提供个股基本面因子数据。"
            "请使用 TushareDataFetcher 获取 A 股因子数据。"
        )


# ============================================================
# 真实数据源接入：Tushare Pro（A 股基本面与 Alpha 因子）
# ============================================================

class TushareDataFetcher(DataFetcher):
    """
    Tushare Pro A 股数据获取器

    职责：
    - 拉取 A 股基本面因子（PE, PB, ROE 等）
    - 拉取 A 股日线/分钟线 OHLCV（备选通道）

    风控红线（前视偏差）：
    - 财报类数据 **必须且只能** 使用 ann_date（公告日）作为时间索引
    - 绝对禁止使用 end_date（报告期）作为索引
    - 如果财报季前没有新数据，必须交由下游 ffill，绝不能让策略看到未来的财报

    异常防御：
    - Tushare 积分不足 → 捕获并返回空 DataFrame
    - 限频（每分钟 200 次）→ 捕获 429 并记录
    - 停牌/退市导致的空数据 → 显式日志警告
    """

    # Tushare 基本面因子 → API 接口与字段映射
    # key: 因子名（策略层使用），value: (api_name, field_name)
    FACTOR_FIELD_MAP: Dict[str, tuple] = {
        "pe": ("daily_basic", "pe"),          # 滚动市盈率
        "pe_ttm": ("daily_basic", "pe_ttm"),  # TTM 市盈率
        "pb": ("daily_basic", "pb"),           # 市净率
        "ps": ("daily_basic", "ps"),           # 市销率
        "ps_ttm": ("daily_basic", "ps_ttm"),   # TTM 市销率
        "dv_ratio": ("daily_basic", "dv_ratio"), # 股息率
        "total_mv": ("daily_basic", "total_mv"), # 总市值
        "circ_mv": ("daily_basic", "circ_mv"),   # 流通市值
        # 以下为财报类因子 — 强制使用 ann_date
        "roe": ("fina_indicator", "roe"),       # 净资产收益率
        "roa": ("fina_indicator", "roa"),       # 总资产收益率
        "grossprofit_margin": ("fina_indicator", "grossprofit_margin"), # 毛利率
        "netprofit_margin": ("fina_indicator", "netprofit_margin"),     # 净利率
        "debt_to_assets": ("fina_indicator", "debt_to_assets"),         # 资产负债率
        "current_ratio": ("fina_indicator", "current_ratio"),           # 流动比率
        "quick_ratio": ("fina_indicator", "quick_ratio"),               # 速动比率
        "ar_turn": ("fina_indicator", "ar_turn"),                       # 应收账款周转率
        "inv_turn": ("fina_indicator", "inv_turn"),                     # 存货周转率
    }

    # 标记哪些因子属于财报类 — 这些因子的时间索引必须使用 ann_date
    REPORT_TYPE_FACTORS: set = {
        "roe", "roa", "grossprofit_margin", "netprofit_margin",
        "debt_to_assets", "current_ratio", "quick_ratio",
        "ar_turn", "inv_turn",
    }

    def __init__(self, token: Optional[str] = None):
        """
        初始化 Tushare Pro 数据获取器

        参数：
            token: Tushare Pro Token，为 None 时从 config 层自动加载

        异常：
            ValueError: Token 缺失时抛出
        """
        # 优先代理 tnskhdata（10000 积分，TNSKHDATA_TOKEN），回退直连 tushare（token 参数）。
        # token 参数仅直连兜底时生效；代理模式下由 .env TNSKHDATA_TOKEN 决定，忽略此参数。
        try:
            from data._tushare_compat import get_pro, source_name
            self._pro = get_pro()
            logger.info("Tushare Pro API 客户端初始化成功（源=%s）", source_name())
        except ImportError:
            raise ImportError(
                "tushare/tnskhdata 均未安装。请执行：pip install tushare tnskhdata"
            )
        except Exception as e:
            raise ConnectionError(
                f"Tushare Pro API 初始化失败：{e}。请检查 Token 是否有效。"
            )

        # 限频计数器：Tushare 每分钟最多约 200 次请求
        self._request_count = 0
        self._cache: Dict[str, pd.DataFrame] = {}

    def fetch_ohlcv(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        freq: str = "1d"
    ) -> pd.DataFrame:
        """
        从 Tushare 拉取 A 股 OHLCV 数据

        参数：
            symbol: Tushare 格式代码（如 "600000.SH"）
            start: 起始时间
            end: 结束时间
            freq: 频率（仅支持 "1d"，分钟线需更高积分）

        返回：
            DataFrame with index: DatetimeIndex (tz-aware, Asia/Shanghai)
            columns: ['open', 'high', 'low', 'close', 'volume', 'amount']

        注意：
            Tushare 日线接口返回 trade_date 列（格式 YYYYMMDD），
            需要手动转换为 DatetimeIndex 并添加时区。
        """
        # ── 本地 Parquet 缓存查询（必须最先做）──
        # 【关键】限流 acquire 与熔断守卫必须放在缓存命中检查之后：
        # 否则缓存命中也会白白扣令牌，导致高缓存命中率的并发回测被无谓限流。
        cache_id = f"ohlcv_{symbol}"
        cached = _read_parquet_cache("tushare", cache_id, start, end)
        if cached is not None:
            return cached

        # 【限流】仅当缓存未命中、即将调真实 API 时才取令牌 —— 防 Tushare 限频封禁
        # Tushare 限频触发后会封禁 IP 一段时间，令牌桶在源头上削峰
        tushare_rate_limiter.acquire(1.0)
        # 【熔断守卫】OPEN 则快速返回空 DF（保留既有"不抛"契约）
        # 原因：Tushare 限频冷却期内继续打只会延长封禁，OPEN 即停打
        if not tushare_breaker.allow_request():
            logger.warning(f"Tushare 熔断开启，跳过日线请求：{symbol}")
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "amount"],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        if freq != "1d":
            logger.warning(
                f"Tushare 分钟线需要更高积分，当前仅支持日频。"
                f"请求频率 [{freq}] 将被忽略，返回日频数据。"
            )

        try:
            # 真正触达 Tushare API 的逻辑抽到 _fetch_ohlcv_from_api，便于异常分类
            df = self._fetch_ohlcv_from_api(symbol, start, end)
            # 仅当真实 API 返回非空数据时才计一次成功 → 复位熔断计数。
            # 空数据（停牌/退市/无数据）属于中性事件：既不算成功也不算失败，
            # 否则"429 与空数据交替"会不断复位计数，熔断永远不跳闸。
            # （DataFetchError 契约：无数据不计熔断 —— 中性语义最贴合）
            if not df.empty:
                tushare_breaker.record_success()
        except Exception as e:
            error_msg = str(e)
            # 基础设施类（限频/超时/连接）异常 → 计入熔断
            # 这类异常在冷却期内大概率持续，熔断可自动停打、缩短封禁时间
            if ("频繁" in error_msg or "limit" in error_msg.lower()
                    or "timeout" in error_msg.lower()
                    or "connection" in error_msg.lower()):
                logger.error(f"Tushare API 限频/网络异常：{symbol}")
                tushare_breaker.record_failure()
            elif "积分" in error_msg or "权限" in error_msg:
                # 积分/权限为持久态异常 —— 60s 冷却内不可恢复，熔断无意义，仅记日志
                logger.error(f"Tushare 积分不足/权限受限：{symbol} - {error_msg}")
            else:
                logger.error(f"Tushare 日线拉取失败 [{symbol}]：{error_msg}")
            # 保留既有契约：统一返回空 DataFrame，不抛
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "amount"],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        self._request_count += 1

        # ── 写入本地 Parquet 缓存 ──
        _write_parquet_cache("tushare", cache_id, start, end, df)

        return df

    def _fetch_ohlcv_from_api(
        self, symbol: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        """
        真正调用 Tushare daily 接口拉取日线，并组装为系统标准 DataFrame。

        抽离动机：把"触达外部 API + 数据清洗组装"与"缓存读写 + 异常分类 +
        熔断/限流守卫"解耦。本方法只负责取数与格式标准化，任何异常一律向上
        抛给 fetch_ohlcv，由其统一分类（基础设施 vs 持久态）并决定是否计入熔断。

        取数数学/数据逻辑（与改造前完全一致，未做任何调整）：
        - trade_date YYYYMMDD → DatetimeIndex（正序）
        - 列名小写：open/high/low/close/volume/amount（vol→volume 改名）
        - tz 本地化为 Asia/Shanghai
        - 成交额单位 千元 → 元（×1000）
        - 过滤到 [start, end] 请求范围
        """
        # Tushare 日线接口：ts_code 格式如 600000.SH
        # trade_date 格式：YYYYMMDD 字符串
        df = self._pro.daily(
            ts_code=symbol,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d")
        )

        if df is None or df.empty:
            logger.warning(f"Tushare 日线数据为空 [{symbol}]，可能停牌或退市")
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "amount"],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        # ── 时间索引转换（纯向量化，无 for 循环）──
        # Tushare 的 trade_date 是 YYYYMMDD 字符串，批量转换为 datetime
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
        df = df.set_index("trade_date")
        df = df.sort_index()  # Tushare 默认倒序，需正序排列

        # 仅保留系统标准列
        standard_cols = ["open", "high", "low", "close", "vol", "amount"]
        available_cols = [c for c in standard_cols if c in df.columns]
        df = df[available_cols]

        # 统一列名：Tushare 用 "vol"，系统标准用 "volume"
        if "vol" in df.columns:
            df = df.rename(columns={"vol": "volume"})

        # 添加 Asia/Shanghai 时区
        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Shanghai")
        else:
            df.index = df.index.tz_convert("Asia/Shanghai")

        # 成交额单位：Tushare 返回千元，系统标准为元
        if "amount" in df.columns:
            df["amount"] = df["amount"] * 1000

        # 过滤到请求范围
        mask = (df.index >= pd.Timestamp(start, tz="Asia/Shanghai")) & \
               (df.index <= pd.Timestamp(end, tz="Asia/Shanghai"))
        df = df.loc[mask]

        return df

    def fetch_macro(
        self,
        indicator: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """Tushare 宏观数据接口受限，建议使用 FRED"""
        raise NotImplementedError(
            "Tushare 宏观数据接口覆盖有限。"
            "请使用 FredDataFetcher 获取宏观经济指标。"
        )

    def fetch_factor_data(
        self,
        symbol: str,
        factor_name: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """
        从 Tushare 拉取 A 股基本面因子数据

        参数：
            symbol: Tushare 格式代码（如 "600000.SH"）
            factor_name: 因子名称（如 "pe", "pb", "roe"）
            start: 起始时间
            end: 结束时间

        返回：
            DataFrame with index: DatetimeIndex (tz-aware, Asia/Shanghai)
            columns: [factor_name]

        ── 风控红线：前视偏差防范 ──
        ┌──────────────┬────────────────────────────────────────────────────┐
        │ 因子类型     │ 时间索引规则                                      │
        ├──────────────┼────────────────────────────────────────────────────┤
        │ 日频估值因子 │ trade_date（交易日当日可见，如 PE 每日更新）        │
        │ (pe/pb/ps等) │ 无需额外滞后                                      │
        ├──────────────┼────────────────────────────────────────────────────┤
        │ 财报类因子   │ ann_date（公告日）—— 绝对禁止用 end_date！          │
        │ (roe/roa等)  │ 如果某财报季尚未发布，下游必须 ffill，不可向前填充  │
        └──────────────┴────────────────────────────────────────────────────┘
        """
        if factor_name not in self.FACTOR_FIELD_MAP:
            logger.error(
                f"未知因子名称：{factor_name}。"
                f"支持的因子：{list(self.FACTOR_FIELD_MAP.keys())}"
            )
            return pd.DataFrame(
                columns=[factor_name],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        # ── 本地 Parquet 缓存查询 ──
        # 基本面/财报因子更新频率低（日频估值 / 季频财报），缓存命中率极高。
        # 相同 (symbol, factor_name, start, end) 的第二次请求直接毫秒级读取，
        # 既保护 Token 又节省 Tushare 积分（fina_indicator 按调用计费）。
        cache_id = f"factor_{symbol}_{factor_name}"
        cached = _read_parquet_cache("tushare", cache_id, start, end)
        if cached is not None:
            return cached

        # 【限流】外层只取一次令牌 —— daily_basic / fina_indicator 两个 helper
        # 不再各自 acquire，避免一次请求被双重计数（brief 明确禁止双重计数）。
        tushare_rate_limiter.acquire(1.0)

        api_name, field_name = self.FACTOR_FIELD_MAP[factor_name]

        # ── 分支 1：日频估值因子（daily_basic）──
        if api_name == "daily_basic":
            df = self._fetch_daily_basic_factor(symbol, field_name, factor_name, start, end)

        # ── 分支 2：财报类因子（fina_indicator）—— 强制使用 ann_date ──
        elif api_name == "fina_indicator":
            df = self._fetch_report_factor(symbol, field_name, factor_name, start, end)

        else:
            # 兜底：未知接口类型
            df = pd.DataFrame(
                columns=[factor_name],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        # ── 写入本地 Parquet 缓存 ──
        # 空 DataFrame 不写入（_write_parquet_cache 内部已防御），
        # 确保拉取失败时下次请求仍重试 API，而非永久缓存空结果。
        _write_parquet_cache("tushare", cache_id, start, end, df)

        return df

    def _fetch_daily_basic_factor(
        self,
        symbol: str,
        field_name: str,
        factor_name: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """
        拉取 daily_basic 接口的日频估值因子

        daily_basic 接口返回每日更新的估值指标（PE/PB/PS等），
        时间字段为 trade_date，无需额外发布滞后。
        """
        try:
            # 逐日拉取会极慢，Tushare 支持按日期范围批量拉取
            # 但 daily_basic 每次请求最多返回某一天全市场数据
            # 因此需要按交易日遍历 —— 这是 Tushare API 的硬性限制
            # 优化策略：只拉取有数据的日期范围，避免无效请求
            df = self._pro.daily_basic(
                ts_code=symbol,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                fields=f"ts_code,trade_date,{field_name}"
            )
        except Exception as e:
            error_msg = str(e)
            if "积分" in error_msg or "权限" in error_msg:
                logger.error(f"Tushare 积分不足：{symbol}.{factor_name} - {error_msg}")
            elif "频繁" in error_msg or "limit" in error_msg.lower():
                logger.error(f"Tushare API 限频：{symbol}.{factor_name}")
                # 限频属基础设施异常 → 计熔断（限流由外层 fetch_factor_data 统一取令牌，勿双重计数）
                tushare_breaker.record_failure()
            else:
                logger.error(f"Tushare daily_basic 拉取失败 [{symbol}.{factor_name}]：{error_msg}")
            return pd.DataFrame(
                columns=[factor_name],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        if df is None or df.empty:
            logger.warning(f"Tushare daily_basic 数据为空 [{symbol}.{factor_name}]，可能停牌")
            return pd.DataFrame(
                columns=[factor_name],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        # ── 时间索引转换 ──
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
        df = df.set_index("trade_date")
        df = df.sort_index()
        df = df[[field_name]].rename(columns={field_name: factor_name})

        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Shanghai")
        else:
            df.index = df.index.tz_convert("Asia/Shanghai")

        # 过滤时间范围
        mask = (df.index >= pd.Timestamp(start, tz="Asia/Shanghai")) & \
               (df.index <= pd.Timestamp(end, tz="Asia/Shanghai"))
        df = df.loc[mask]

        self._request_count += 1
        return df

    def _fetch_report_factor(
        self,
        symbol: str,
        field_name: str,
        factor_name: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """
        拉取 fina_indicator 接口的财报类因子

        ── 风控红线 ──
        时间索引 **必须且只能** 使用 ann_date（公告日）。

        为什么不能用 end_date？
        end_date 是报告期（如 2024-03-31 表示一季报），
        但一季报通常在 4 月底才公告。如果用 end_date 做索引，
        策略在 4 月初就能"看到"一季报数据 → 前视偏差！

        极端防御场景：
        1. ann_date 为 NaN：部分早期财报可能缺失公告日，该行数据必须丢弃
        2. 同一报告期有多条记录：保留 ann_date 最晚的一条（修正公告）
        3. ann_date 早于 start：虽然是旧数据，但可能在该期间首次被策略使用
        4. 退市/ST 公司：财报可能缺失或异常，返回空 DataFrame 而非崩溃
        """
        try:
            # 请求范围向前扩展 1 年 —— 因为策略在 start 时刻可能需要
            # 回溯到上一次已公告的财报数据（由下游 ffill 补齐）
            extended_start = pd.Timestamp(start) - pd.DateOffset(years=1)

            df = self._pro.fina_indicator(
                ts_code=symbol,
                start_date=extended_start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                fields=f"ts_code,ann_date,end_date,{field_name}"
            )
        except Exception as e:
            error_msg = str(e)
            if "积分" in error_msg or "权限" in error_msg:
                logger.error(f"Tushare 积分不足：{symbol}.{factor_name} - {error_msg}")
            elif "频繁" in error_msg or "limit" in error_msg.lower():
                logger.error(f"Tushare API 限频：{symbol}.{factor_name}")
                # 限频属基础设施异常 → 计熔断（限流由外层 fetch_factor_data 统一取令牌，勿双重计数）
                tushare_breaker.record_failure()
            else:
                logger.error(f"Tushare fina_indicator 拉取失败 [{symbol}.{factor_name}]：{error_msg}")
            return pd.DataFrame(
                columns=[factor_name],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        if df is None or df.empty:
            logger.warning(f"Tushare 财报数据为空 [{symbol}.{factor_name}]，可能停牌/退市")
            return pd.DataFrame(
                columns=[factor_name],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        # ── 极端防御 1：丢弃 ann_date 为 NaN 的行 ──
        # 这些行的公告日信息缺失，无法确定策略何时可见，必须丢弃
        nan_ann_count = df["ann_date"].isna().sum()
        if nan_ann_count > 0:
            logger.warning(
                f"财报因子 [{symbol}.{factor_name}] 有 {nan_ann_count} 行缺失 ann_date，"
                f"这些行将被丢弃（无法确定公告日 = 无法防范前视偏差）"
            )
        df = df.dropna(subset=["ann_date"])

        if df.empty:
            logger.warning(f"财报因子 [{symbol}.{factor_name}] 丢弃 NaN ann_date 后为空")
            return pd.DataFrame(
                columns=[factor_name],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        # ── 极端防御 2：同一 end_date 多条记录 → 保留 ann_date 最晚的 ──
        # 这处理"修正公告"场景：公司可能在原公告后发布修正版
        # 策略应在修正公告日才看到修正后的数据
        dup_mask = df.duplicated(subset=["end_date"], keep="last")
        if dup_mask.any():
            dup_count = dup_mask.sum()
            logger.info(
                f"财报因子 [{symbol}.{factor_name}] 检测到 {dup_count} 条重复报告期记录，"
                f"保留 ann_date 最晚的（修正公告优先）"
            )
        df = df.drop_duplicates(subset=["end_date"], keep="last")

        # ── 核心风控：使用 ann_date 作为时间索引 ──
        # 绝对禁止使用 end_date！这里是整条防线的咽喉
        df["ann_date"] = pd.to_datetime(df["ann_date"], format="%Y%m%d")
        df = df.set_index("ann_date")
        df = df.sort_index()
        df = df[[field_name]].rename(columns={field_name: factor_name})

        # 添加时区
        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Shanghai")
        else:
            df.index = df.index.tz_convert("Asia/Shanghai")

        # ── 过滤到请求范围 ──
        # 注意：虽然请求时扩展了 1 年，但返回时只保留 [start, end] 范围
        # 这样做是为了让下游 ffill 有足够的起始锚点
        mask = (df.index >= pd.Timestamp(start, tz="Asia/Shanghai")) & \
               (df.index <= pd.Timestamp(end, tz="Asia/Shanghai"))
        df = df.loc[mask]

        self._request_count += 1
        return df


# ============================================================
# 组合模式聚合网关：双轨数据架构
# ============================================================

class CompositeDataFetcher(DataFetcher):
    """
    聚合数据获取网关（组合模式）

    架构设计：
    ┌─────────────────────────────────────────────────┐
    │              CompositeDataFetcher                │
    │  ┌─────────────┬────────────┬─────────────────┐ │
    │  │  QMT/Mock   │   FRED     │   Tushare       │ │
    │  │  (快车道)    │  (慢车道)   │   (慢车道)      │ │
    │  │  OHLCV      │  宏观锚点   │   基本面因子     │ │
    │  └─────────────┴────────────┴─────────────────┘ │
    └─────────────────────────────────────────────────┘

    路由规则：
    - fetch_ohlcv       → QMT/Mock（快车道：实盘 Tick/分钟/日频量价）
    - fetch_macro       → FRED（慢车道：宏观锚点，日/月/季频）
    - fetch_factor_data → Tushare（慢车道：基本面因子，日/季频）

    统一保障：
    - 所有返回 DataFrame 的 time index 一律为 tz-aware (Asia/Shanghai)
    - 列名严格遵循系统标准（见 DataFetcher 基类文档）
    - 任何底层 fetcher 异常不向上冒泡，返回空 DataFrame + 日志
    """

    def __init__(
        self,
        qmt_fetcher: DataFetcher,
        fred_fetcher: DataFetcher,
        tushare_fetcher: DataFetcher
    ):
        """
        构造聚合网关

        参数：
            qmt_fetcher: QMT/Mock 行情数据获取器（快车道）
            fred_fetcher: FRED 宏观数据获取器（慢车道）
            tushare_fetcher: Tushare A 股因子获取器（慢车道）
        """
        self._qmt = qmt_fetcher
        self._fred = fred_fetcher
        self._tushare = tushare_fetcher
        logger.info(
            f"聚合网关初始化完成："
            f"QMT={type(qmt_fetcher).__name__}, "
            f"FRED={type(fred_fetcher).__name__}, "
            f"Tushare={type(tushare_fetcher).__name__}"
        )

    def fetch_ohlcv(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        freq: str = "1d"
    ) -> pd.DataFrame:
        """
        路由到 QMT/Mock 获取 OHLCV 数据

        快车道：实盘 Tick / 分钟 / 日频量价数据
        """
        try:
            df = self._qmt.fetch_ohlcv(symbol, start, end, freq)
        except Exception as e:
            logger.error(f"QMT/Mock OHLCV 拉取失败 [{symbol}]：{e}")
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "amount"],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        # 统一保障：确保 tz-aware
        df = self._ensure_tz_aware(df)
        return df

    def fetch_macro(
        self,
        indicator: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """
        路由到 FRED 获取宏观数据

        慢车道：宏观锚点数据，已内置发布滞后防前视偏差
        """
        try:
            df = self._fred.fetch_macro(indicator, start, end)
        except NotImplementedError as e:
            # 底层 fetcher 不支持该接口（如 MockDataFetcher）
            logger.warning(f"宏观数据源不支持该请求：{e}")
            return pd.DataFrame(
                columns=[indicator],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )
        except Exception as e:
            logger.error(f"FRED 宏观数据拉取失败 [{indicator}]：{e}")
            return pd.DataFrame(
                columns=[indicator],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        # 统一保障：确保 tz-aware
        df = self._ensure_tz_aware(df)
        return df

    def fetch_factor_data(
        self,
        symbol: str,
        factor_name: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """
        路由到 Tushare 获取 A 股基本面因子

        慢车道：财报类因子已强制使用 ann_date，防范前视偏差
        """
        try:
            df = self._tushare.fetch_factor_data(symbol, factor_name, start, end)
        except NotImplementedError as e:
            logger.warning(f"因子数据源不支持该请求：{e}")
            return pd.DataFrame(
                columns=[factor_name],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )
        except Exception as e:
            logger.error(f"Tushare 因子数据拉取失败 [{symbol}.{factor_name}]：{e}")
            return pd.DataFrame(
                columns=[factor_name],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai")
            )

        # 统一保障：确保 tz-aware
        df = self._ensure_tz_aware(df)
        return df

    @staticmethod
    def _ensure_tz_aware(df: pd.DataFrame) -> pd.DataFrame:
        """
        确保 DataFrame 的时间索引是 tz-aware (Asia/Shanghai)

        这是聚合网关的统一保障层：
        - tz-naive → 本地化为 Asia/Shanghai
        - 其他时区 → 转换为 Asia/Shanghai
        - 空 DataFrame → 直接返回（避免操作空索引）
        """
        if df.empty:
            return df

        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Shanghai")
        elif str(df.index.tz) != "Asia/Shanghai":
            df.index = df.index.tz_convert("Asia/Shanghai")

        return df