"""JQData 单例客户端：threading.Lock 防并发（聚宽单连接）+ 配额双机制
（手动计数 + get_query_count 校准）+ 钉钉告警。

红线（聚宽试用账号限制：每日 100 万条 + 单连接）：
  - 单连接：聚宽试用账号严禁并发拉取，故 fetch 全程持有 threading.Lock 串行化；
  - 配额双机制：本地手动计数 + 每 N 次用服务端 get_query_count 校准，二者任一
    触临限即抛 QuotaExceeded + 钉钉告警，绝不超日限额（越界即扣费/封号）；
  - 缺凭证降级：未配 JQDATA_USERNAME/PASSWORD 时返空 DataFrame，绝不抛到核心引擎；
  - money→amount 洗净 + tz-naive DatetimeIndex：防范时区错配导致 join 异常。

为何不引入重型量化库：本类只做「拉取 + 配额闸门 + 洗净」，向量化/因子计算
属上游职责，显式平铺直叙，无黑盒。
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import threading

import pandas as pd

from config import JQDATA_CONFIG
from core.notifier import NotificationManager, fire_and_forget

logger = logging.getLogger(__name__)


class QuotaExceeded(Exception):
    """聚宽日额度临限异常：手动计数或服务端 spare 触红线时抛出。"""
    ...


class JQDataClient:
    """聚宽分钟级安全客户端（单例 + 锁 + 配额双机制）。

    设计意图（三道防线缺一不可）：
      1) threading.Lock —— 聚宽试用账号仅允许单连接，并发拉取会被服务端拒/封，
         故 fetch 全程持锁串行化，是最底层的一致性保障；
      2) 手动计数 _today_count —— 每次 get_price 按 len(df) 累加，到 95 万硬停
         （留 5 万余量兜底），不依赖服务端计数（get_query_count 偶发失败时仍能兜底）；
      3) get_query_count 校准 —— 每 calibrate_every(10) 次用服务端权威计数复位
         _today_count，纠偏本地累计漂移（部分成交/网络重试/计数口径差异）。
      任一防线触临限即抛 QuotaExceeded + 钉钉告警，停止拉取，绝不越界。
    """

    _instance: "JQDataClient | None" = None
    _singleton_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "JQDataClient":
        """双重检查锁单例（线程安全）。"""
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        # Why 单独的 fetch 锁：单例只保证「同一对象」，但聚宽单连接要求「同一时刻
        # 只有一个请求在途」，故每次 fetch 全程持锁，从入口到 return/raise。
        self._lock = threading.Lock()
        # 缺凭证即降级：不抛、不认证，fetch_minute_bars 直接返空 DF。
        self._enabled = bool(
            os.getenv("JQDATA_USERNAME") and os.getenv("JQDATA_PASSWORD"))
        self._today_count: int = 0
        self._today: _dt.date = _dt.date.today()
        # 校准计数器：初始 0 → 首次 fetch 即触发校准（拿权威 spare 复位计数）
        self._calls_since_calib: int = 0
        if self._enabled:
            try:
                import jqdatasdk as jq
                jq.auth(os.getenv("JQDATA_USERNAME"), os.getenv("JQDATA_PASSWORD"))
                logger.info("JQData 认证成功")
            except Exception as e:
                # 认证失败降级：绝不阻断主流程，后续 fetch 返空 DF。
                logger.error("JQData 认证失败，降级返空 DataFrame：%s", e)
                self._enabled = False

    # ------------------------------------------------------------------
    # 配额管理
    # ------------------------------------------------------------------

    def _reset_if_new_day(self) -> None:
        """跨日复位：聚宽配额按自然日重置（每日 100 万条），跨日必须清零计数。"""
        today = _dt.date.today()
        if today != self._today:
            self._today = today
            self._today_count = 0
            self._calls_since_calib = 0

    def _spare(self) -> float:
        """查询服务端剩余条数（权威）。失败时保守返 1_000_000 避免误判临限
        （误判会阻断拉取，但 get_query_count 失败不代表真的超额）。"""
        try:
            import jqdatasdk as jq
            return float(jq.get_query_count().get("spare", 1_000_000))
        except Exception as e:
            logger.warning("JQData get_query_count 失败，保守按 100 万处理：%s", e)
            return 1_000_000.0

    def _calibrate(self) -> None:
        """用服务端权威计数复位本地 _today_count。

        Why max(本地, 1_000_000 - spare)：spare 是服务端剩余，total-spare 即已用条数，
        是最权威的消耗值；但若服务端计数因延迟/口径小于本地累计，取 max 避免回退
        （回退会让红线形同虚设）。校准后复位 _calls_since_calib 重新计时。
        """
        spare = self._spare()
        used = 1_000_000 - spare
        self._today_count = max(self._today_count, used)
        self._calls_since_calib = 0

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def fetch_minute_bars(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        frequency: str = "5m",
    ) -> pd.DataFrame:
        """拉取分钟级 OHLCV（前复权）。

        参数：
            symbol:     聚宽代码（如 "000001.SZ"）
            start_date: 起始日 "YYYY-MM-DD"
            end_date:   结束日（含）
            frequency:  "1m" / "5m"（默认 5m，分钟级数据湖上游）

        返回：
            DataFrame[open,high,low,close,volume,amount]，tz-naive DatetimeIndex，
            按时间升序。缺凭证/拉取失败/空结果均返空 DataFrame（绝不抛到核心引擎，
            除临限 QuotaExceeded 外）。
        """
        if not self._enabled:
            return pd.DataFrame()
        # 单连接串行：从持锁到 return/raise，杜绝并发被服务端拒。
        with self._lock:
            self._reset_if_new_day()
            # 校准周期判定：首次调用（_calls_since_calib==0）或每 calibrate_every 次，
            # 先用服务端权威计数复位 _today_count，再做临限判定。
            # Why 先校准后判临限：校准后计数最准，避免用漂移的本地计数误判。
            if self._calls_since_calib == 0 \
                    or self._calls_since_calib >= JQDATA_CONFIG["calibrate_every"]:
                self._calibrate()
            # 临限判定（双机制红线）：手动计数>=95万 或 服务端spare<5万 → 抛+告警。
            # Why 必须抛 + 告警：越界即扣费/封号，是聚宽试用账号最致命的边界；
            # 钉钉告警让人工立即介入，QuotaExceeded 让调用方显式停拉。
            if self._today_count >= JQDATA_CONFIG["quota_manual_limit"] \
                    or self._spare() < JQDATA_CONFIG["quota_warn_spare"]:
                fire_and_forget(NotificationManager.get_default().notify_risk_event(
                    f"JQData 日额度将尽（已用≈{self._today_count}，spare≈{int(self._spare())}），"
                    f"已停止拉取 {symbol}", "WARN"))
                raise QuotaExceeded("JQData 日额度接近上限")
            # 拉取（前复权 fq='pre'，分钟级 OHLCV + money）
            try:
                import jqdatasdk as jq
                df = jq.get_price(
                    symbol, start_date=start_date, end_date=end_date,
                    frequency=frequency,
                    fields=["open", "high", "low", "close", "volume", "money"],
                    fq="pre", skip_paused=False)
                if df is None or df.empty:
                    return pd.DataFrame()
                # 配额计数：按实际拉取行数累加（聚宽按条计费）
                self._today_count += len(df)
                self._calls_since_calib += 1
                return self._cleanse(df)
            except QuotaExceeded:
                raise
            except Exception as e:
                # 任意拉取异常降级返空，绝不外泄（核心引擎不容许数据源抛异常）
                logger.error("JQData fetch 失败 [%s]：%s", symbol, e)
                return pd.DataFrame()

    @staticmethod
    def _cleanse(df: pd.DataFrame) -> pd.DataFrame:
        """洗净：money→amount 改名 + tz-naive DatetimeIndex + 升序排序。

        Why money→amount：聚宽返「money」（成交额，元），统一改名为 amount
        与下游数据湖 schema 对齐（minute lake: open/high/low/close/volume/amount）。
        Why tz-naive：宏观锚点与 A 股 join 时，时区不一致会触发 pandas 对齐异常；
        统一去时区让上游显式 localize（Asia/Shanghai），避免隐式时区陷阱。
        """
        out = df.copy()
        if "money" in out.columns:
            out = out.rename(columns={"money": "amount"})
        if not isinstance(out.index, pd.DatetimeIndex):
            out.index = pd.to_datetime(out.index)
        # 去时区：聚宽返 Asia/Shanghai tz-aware，统一剥离为 tz-naive
        if getattr(out.index, "tz", None) is not None:
            out.index = out.index.tz_localize(None)
        return out.sort_index()
