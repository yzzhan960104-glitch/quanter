"""Alpha Vantage 客户端：美债收益率（TREASURY_YIELD）。

设计要点：
- 叠加 @RateLimiter(5/60s) + @CircuitBreaker 双装饰器（spec 要求）。
- 装饰器路径下失败抛 DataFetchError（计入熔断）；对外提供 _safe service 方法
  捕获 CircuitOpenError/DataFetchError → 返回空 DF，守住"绝不抛到核心"红线。
- on_open 跨线程告警用 fire_and_forget。
- 对外只吐纯净 DataFrame：对齐 DatetimeIndex、数值列、剔 NaN。
"""
from __future__ import annotations

import logging
import os

import httpx
import pandas as pd

from core.notifier import NotificationManager, fire_and_forget
from data.resilience import CircuitBreaker, DataFetchError, RateLimiter

# 复用熔断器在装饰器路径抛出的异常类型
from data.resilience import CircuitOpenError

logger = logging.getLogger(__name__)

_EMPTY_TY = pd.DataFrame(index=pd.DatetimeIndex([]))


def _notify_av_open() -> None:
    fire_and_forget(
        NotificationManager.get_default().notify_risk_event(
            "Alpha Vantage 接口熔断（连续失败），已暂停拉取", "WARN"))


# 令牌桶：5 calls/60s → capacity=5, refill_rate=5/60
av_limiter = RateLimiter(name="alpha_vantage", capacity=5, refill_rate=5.0 / 60.0)
av_breaker = CircuitBreaker(
    name="alpha_vantage", failure_threshold=3, recovery_timeout=60.0,
    expected_exception=DataFetchError, on_open=_notify_av_open)


class AlphaVantageClient:
    """美债收益率客户端（TREASURY_YIELD）。"""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.getenv("ALPHA_VANTAGE_API_KEY", "")
        self._enabled = bool(self._api_key)

    @av_limiter
    @av_breaker
    async def get_treasury_yield(self, maturity: str,
                                 start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """拉取指定期限美债收益率。失败抛 DataFetchError（供熔断统计）。"""
        if not self._enabled:
            return _EMPTY_TY.copy()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://www.alphavantage.co/query",
                    params={"function": "TREASURY_YIELD", "interval": "daily",
                            "maturity": maturity, "apikey": self._api_key})
                resp.raise_for_status()
                data = resp.json()
            return self._cleanse(data, maturity)
        except Exception as e:
            logger.error("Alpha Vantage 拉取失败 [%s]：%s", maturity, e)
            raise DataFetchError(f"Alpha Vantage: {e}") from e

    async def get_treasury_yield_safe(self, maturity: str,
                                      start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """service 兜底：捕获熔断/限流/拉取异常 → 返回空 DF，绝不抛到核心。"""
        try:
            return await self.get_treasury_yield(maturity, start, end)
        except (CircuitOpenError, DataFetchError) as e:
            logger.warning("Alpha Vantage 降级返回空 DF [%s]：%s", maturity, e)
            return _EMPTY_TY.copy()

    @staticmethod
    def _cleanse(data: dict, maturity: str) -> pd.DataFrame:
        items = data.get("data", []) if isinstance(data, dict) else []
        if not items:
            return _EMPTY_TY.copy()
        df = pd.DataFrame(items)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        df[maturity] = pd.to_numeric(df["value"], errors="coerce")
        return df[[maturity]].dropna()
