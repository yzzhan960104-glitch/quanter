"""YFinance 客户端：标普/原油/黄金/VIX 历史日线。

设计要点（对齐架构师红线）：
- yfinance 库本身同步 → 用【手动熔断 API】（allow_request/record_*），失败返回空 DF，
  绝不抛到核心引擎。Why 不用装饰器：装饰器路径失败会抛 CircuitOpenError，
  会破坏 fetcher 类组件"任何异常都返回空 DataFrame"的对外契约；手动 API 让我们
  在 OPEN 时直接 return _EMPTY.copy()，彻底封死异常外泄路径。
- on_open 跨线程告警：熔断跳闸发生在 yf.download 的同步调用线程，这类工作线程
  无运行中的事件循环。若直接 asyncio.create_task(notify_risk_event(...)) 会抛
  RuntimeError("no running event loop")，导致风控告警被静默吞掉。
  必须用 fire_and_forget —— 它起一个 daemon 线程跑独立 asyncio.run，
  是跨线程触发异步告警的最简显式做法，不阻塞调用方主流程。
- 对外只吐纯净 DataFrame：对齐无时区 DatetimeIndex + 标准列名 + 剔 NaN。
  Why 去时区：宏观锚点（VIX/黄金）与 A 股策略 join 时，时区不一致会引发
  pandas 对齐异常；统一 tz-naive 让上游显式 localize，避免隐式时区陷阱。
"""
from __future__ import annotations

import logging

import pandas as pd
import yfinance as yf

from core.notifier import NotificationManager, fire_and_forget
from data.resilience import CircuitBreaker

logger = logging.getLogger(__name__)

# 空降级 DataFrame（标准 schema，供熔断/失败时返回）
# Why 用 .copy() 返回：_EMPTY 是模块级共享单例，若直接返回引用，
# 下游若对返回值做原地修改会污染全局空表，引发难以追踪的串味 bug。
_EMPTY = pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                      index=pd.DatetimeIndex([]))


def _notify_yfinance_open() -> None:
    """熔断 on_open 同步回调：后台触发钉钉告警，不阻塞调用线程。

    Why 用 fire_and_forget 而非直接 await：
    on_open 在 CircuitBreaker._fire 中被同步调用（record_failure→_trip_locked→_fire），
    此时处于 yf.download 的同步调用栈，无运行事件循环。fire_and_forget 会起
    daemon 线程跑独立 asyncio.run，跨线程安全投递告警，异常仅记日志不外泄。
    """
    fire_and_forget(
        NotificationManager.get_default().notify_risk_event(
            "yfinance 接口熔断（连续失败），已暂停拉取", "WARN"))


# 模块级熔断器：连续 3 次基础设施异常 → 熔断 60s
# Why 模块级单例：与 tushare_breaker/fred_breaker 范式一致，
# 跨实例共享熔断状态，避免每个 YFinanceClient 实例各持一份导致阈值失真。
yfinance_breaker = CircuitBreaker(
    name="yfinance", failure_threshold=3, recovery_timeout=60.0,
    on_open=_notify_yfinance_open)


class YFinanceClient:
    """标普/原油/黄金/VIX 历史日线客户端。"""

    def get_history(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """拉取日线；熔断/失败一律返回空 DF，绝不抛。

        参数：
            symbol: yfinance 代码（如 ^GSPC、CL=F、GC=F、^VIX）
            start/end: 'YYYY-MM-DD'

        红线契约：
            - 熔断 OPEN 期间不触达 yf.download（防连环超时被封禁）；
            - 任何异常（网络/限频/解析）一律 catch → 返回空 DF，绝不外抛；
            - 返回值始终为标准 schema（open/high/low/close/volume + 无时区 DatetimeIndex）。
        """
        # 熔断守卫：OPEN 则快速返回空 DF，绝不触达底层 yf.download
        if not yfinance_breaker.allow_request():
            logger.warning("yfinance 熔断开启，返回空 DF：%s", symbol)
            return _EMPTY.copy()
        try:
            # auto_adjust=False 保留原始 OHLC（不前复权），宏观锚点无需复权
            # progress=False 关闭 tqdm 进度条，避免污染日志
            raw = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=False)
            if raw is None or raw.empty:
                # 空数据属中性事件（如非交易日范围），不计熔断也不算成功
                return _EMPTY.copy()
            yfinance_breaker.record_success()
            return self._cleanse(raw)
        except Exception as e:
            # 基础设施异常（网络/限频/解析）→ 计入熔断；返回空 DF 绝不抛
            # Why 宽 catch Exception：yfinance 内部异常类型多变（HTTPError/JSONDecodeError
            # /ConnectionError），逐一列举易漏；红线是"绝不外抛"，宽 catch 最稳。
            logger.error("yfinance 拉取失败 [%s]：%s", symbol, e)
            yfinance_breaker.record_failure()
            return _EMPTY.copy()

    @staticmethod
    def _cleanse(raw: pd.DataFrame) -> pd.DataFrame:
        """洗净：扁平化可能的多级列、统一列名、去时区、剔 close 为 NaN 的行。

        Why 这些清洗：
        - MultiIndex 扁平化：yf.download 多 symbol 时返回 (Field, Symbol) 二级列，
          单 symbol 也可能返回 MultiIndex（版本差异），统一取 level 0 扁平化。
        - 列名小写：对齐系统 OHLCV 标准 schema（与 TushareDataFetcher 一致）。
        - 去时区：宏观锚点统一 tz-naive，避免与 A 股 Asia/Shanghai 时区隐式错配。
        - 剔 close NaN：close 是核心字段，NaN 行对策略无意义且会污染 rolling/ffill。
        """
        if isinstance(raw.columns, pd.MultiIndex):
            # 取第 0 级（字段名 Open/High/...），丢弃 symbol 级
            raw.columns = raw.columns.get_level_values(0)
        cols = ["Open", "High", "Low", "Close", "Volume"]
        available = [c for c in cols if c in raw.columns]
        df = raw[available].copy()
        df.columns = [c.lower() for c in available]
        df.index = pd.to_datetime(df.index)
        if getattr(df.index, "tz", None) is not None:
            # 统一无时区：tz-aware → tz_localize(None) 直接剥离时区信息
            df.index = df.index.tz_localize(None)  # 统一无时区
        if "close" in df.columns:
            # close 为 NaN 的行对策略无意义，剔除（不 ffill，避免掩盖数据源异常）
            df = df.dropna(subset=["close"])
        return df
