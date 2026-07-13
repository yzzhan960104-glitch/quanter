"""数据湖读取适配器（多湖缓存）：按 key 缓存多张 parquet，提供截面/时序查询。

多湖改造（本轮）：
- 由单湖（`_df/_ffill/_loaded/_date_dtype`）扩为多湖字典缓存
  （`_lakes/_ffills/_dtypes/_default_key`），支持 Macro/Sector/Daily/1Min/Crypto 等多类资产。
- `load(path, *, key=None)`：key 缺省=path；首个成功 load 的 key 设为 `_default_key`。
- `get_*(..., lake=None)`：lake 缺省=`_default_key`，按 key 路由到对应湖。
- 每湖独立应用既有 ffill/sort/normalize 逻辑，互不串味。

前视偏差拷问（关键，每湖独立复用）：
- 仅对【价格列】沿【时间轴】ffill —— 停牌日沿用末次成交价，安全无前视；
  volume/amount 绝不 ffill（停牌日成交应为 0，ffill 会造假量、污染流动性判断）。
- groupby(level="symbol") 保证 ffill 不跨标的串味：否则会把 A 标的末值填到 B 标的的
  停牌日，制造虚假价格与前视污染。
- ffill 沿时间方向只传播【过去】值到当前，不引入未来信息。

离线降级（向后兼容红线）：
- parquet 缺失时 load() 仅记 warning，不写入缓存；`.loaded` 在无任何湖已载时为 False。
- 无湖时 `get_cross_section`/`get_timeseries` 返回空 DF，绝不 raise ——
  保住既有 `test_offline_mode_when_parquet_missing` 契约与开发机/CI 无湖启动语义。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

import pandas as pd

from config import LAKE_CONFIG

logger = logging.getLogger(__name__)

# 仅价格列参与 ffill；volume/amount 等量类列保持原始值（停牌日 volume=0，
# ffill 会造假量、污染流动性判断）。OHLCV 经典契约。
_PRICE_COLS = ["open", "high", "low", "close"]


class DataLakeReader:
    """单例：多湖常驻内存（每湖独立 ffill/sort/normalize）。"""

    _instance: "DataLakeReader | None" = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "DataLakeReader":
        # 双重检查锁：仿 core/notifier.py 的 NotificationManager.get_default()
        # 保证多线程下仅创建一次实例，避免重复加载 parquet 造成内存翻倍
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        # 多湖缓存：每湖独立存原始 MultiIndex 视图 + 价格已 ffill 的完整视图 + date 层级 dtype。
        # 用 dict 而非嵌套类，保持策略代码扁平化、显式至上（拒绝过度抽象）。
        self._lakes: dict[str, pd.DataFrame] = {}       # key -> 原始 df（MultiIndex(date, symbol)）
        self._ffills: dict[str, pd.DataFrame] = {}      # key -> 价格列已 ffill、量类列保持原值的完整视图
        self._dtypes: dict[str, Any] = {}               # key -> date 层级原生 dtype（_norm_date 归一化依据）
        self._default_key: str | None = None            # 首次成功 load 的 key；lake 缺省时路由到此
        # #3：保护 load() 的 check-then-set，防并发 load 同 key 都过幂等检查 → 重复
        # read_parquet(408MB) 内存翻倍（无 lifespan 的 worker 进程首批并发 scan 的窄窗口）。
        self._load_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        # 任一湖已载即 True；无湖（离线/未 load）即 False。
        return bool(self._lakes)

    def lakes(self) -> list[str]:
        """已载入的湖 key 列表（按 load 顺序）。"""
        return list(self._lakes.keys())

    def symbols(self, lake: str | None = None) -> list[str]:
        """返回指定湖（缺省 default_lake）的全部唯一 symbol 列表。

        用途：caisen_service._load_price_data 全市场枚举入口（universe=None/空 时调用），
        封装私有 _lakes 避免调用方穿透。

        无湖或指定湖不存在 → 返空列表（离线降级，不抛异常，与 get_* 同口径）。
        """
        # _resolve 复用既有路由：显式 lake 优先，否则 config default_lake，回退首个 load 成功的 key。
        key = self._resolve(lake)
        if key is None or key not in self._lakes:
            return []
        # 仅 MultiIndex(date, symbol) 湖有 symbol 层；macro 这类 DatetimeIndex 单索引湖
        # 取不到 symbol 层会抛，故先 isinstance 守卫，单索引湖直接返空（无 symbol 概念）。
        idx = self._lakes[key].index
        if not isinstance(idx, pd.MultiIndex):
            return []
        return list(idx.get_level_values("symbol").unique())

    def get_lake(self, key: str) -> Optional[pd.DataFrame]:
        """公开读：返回指定 key 湖的原始 DataFrame（替代调用方穿透 _lakes 私有，#4）。

        用途：macro 湖（DatetimeIndex 单索引、无 symbol 层）等非标准湖的端点直读——
        get_cross_section/get_timeseries 假设 MultiIndex(date, symbol)，对单索引湖无效，
        故暴露此公开方法让路由层合法读，避免穿透 _lakes。无湖/key 不存在 → 返 None。
        """
        return self._lakes.get(key)

    def load(self, path: str | None = None, *, key: str | None = None) -> None:
        """加载 parquet 到内存（多湖）。#3：委托 _load_impl 并加锁防并发重复 read。

        - key 缺省=path：便于单湖老用法 `load(path)` 自动以 path 为 key，向后兼容。
        - 首个成功 load 的 key 设为 `_default_key`，作为 `get_*(lake=None)` 的默认湖。
        """
        # 锁包 _load_impl 的 check-then-set：锁内含 read_parquet（秒级 IO），load 非热路径
        # （启动/ensure），序列化保证不重复 read；不同 key 也序列化——可接受（启动本就串行）。
        with self._load_lock:
            self._load_impl(path, key=key)

    def _load_impl(self, path: str | None = None, *, key: str | None = None) -> None:
        """实际加载逻辑（多湖）。缺失则进入离线模式（不阻断启动、不写入缓存）。"""
        path = path or LAKE_CONFIG["default_path"]
        key = key or path
        # 幂等守卫：同 key 已 load 不重复 read_parquet（daily 湖 408MB，重复 load 浪费内存+IO）。
        # _load_price_data 的 ensure-load 也用 not reader.loaded 判空，但独立进程多次调用
        # load() 时此守卫是最后一道防线，避免重复 read 进内存。
        if key in self._lakes:
            return
        # 离线降级：parquet 不存在仅记 warning，不写入缓存。设计意图：开发机/CI 无数据湖
        # 时不致启动崩溃，仅降级为空结果（.loaded=False，查询返回空 DF）。
        if not os.path.exists(path):
            logger.warning("数据湖缺失：%s(key=%s)，跳过加载（离线模式）", path, key)
            return
        df = pd.read_parquet(path)
        # 按索引形态分流（修复跨任务硬阻塞 T11 审查发现）：
        # - MultiIndex(date, symbol)：daily/minute/sector 等价格湖，走 xs(symbol)/
        #   groupby(symbol).ffill 价格补齐路径，与既有语义完全一致（不动）。
        # - DatetimeIndex 单索引：macro 湖这类【全市场级别单序列宏观指标】
        #   （shrzgm/M1M2_gap/dr007，无 symbol 概念）。sync_macro_credit 落盘前
        #   已 sort_index + 仅向前 ffill，此处无需二次 groupby；直接缓存进 _lakes，
        #   供 CreditRegime._load_from_lake 用 reader._lakes["macro"].loc[:date]
        #   直读（macro 湖无 symbol 层，get_cross_section/get_timeseries 对其无意义）。
        #   若不在此分支放行，老逻辑会因"非 MultiIndex"直接 logger.error+return，
        #   _lakes["macro"] 永远空 → CreditRegime.compute() 永远返 0 → 宏观否决失效。
        # - 其它索引形态：维持拒绝（语义不明，拒绝早失败优于静默错乱）。
        if isinstance(df.index, pd.MultiIndex):
            df = self._normalize_and_sort(df)
            date_dtype = df.index.get_level_values("date").dtype
            self._lakes[key] = df
            self._ffills[key] = self._build_ffill_view(df)
            self._dtypes[key] = date_dtype
        elif isinstance(df.index, pd.DatetimeIndex):
            # tz 去化 + normalize 到午夜：与 MultiIndex 路径同等对待，防止查询键
            # 带时分秒/tz 与索引键不可比或 silently 切空。
            if df.index.tz is not None:
                df.index = df.index.tz_convert("UTC").tz_localize(None)
            df.index = pd.DatetimeIndex(df.index).normalize()
            df = df.sort_index()
            # 单索引湖无 symbol 层、_series 路径不 xs symbol；ffill 在落盘前已完成，
            # _ffills 这里直接等同 _lakes（避免 get_cross_section 在单索引湖误用，
            # 单索引湖根本不应走 get_cross_section/get_timeseries）。
            self._lakes[key] = df
            self._ffills[key] = df
            self._dtypes[key] = df.index.dtype
        else:
            logger.error("数据湖 %s 索引非 MultiIndex(date, symbol) 亦非 DatetimeIndex，跳过加载", key)
            return
        # 首个成功 load 的 key 锁定为默认湖 —— 保证 `get_*(lake=None)` 老调用路径有确定语义。
        if self._default_key is None:
            self._default_key = key
        logger.info("数据湖加载完成：%s(key=%s)，%d 行", path, key, len(df))

    # ---------- 湖内数据规范化（每湖独立应用，复用既有逻辑） ----------

    @staticmethod
    def _normalize_and_sort(df: pd.DataFrame) -> pd.DataFrame:
        """date 层级去 tz + normalize 到午夜，并对索引排序（每湖独立）。

        Important 2（tz 拷问）：date 层级若为 datetime（含 tz-aware / 非零时间），
        先去时区并 normalize 到午夜，与 _norm_date 查询键对齐，否则 xs/loc 会因 tz
        或时间分量不匹配而 silently 返回空。仅在 datetime 层级生效，str 层级原样保留。
        - tz-aware：先 tz_convert 到 UTC 再 tz_localize(None) 剥离时区，避免直接
          tz_localize(None) 在非 UTC tz 下抛异常；naive 直接跳过 tz 步骤。
        """
        date_vals = df.index.get_level_values("date")
        if isinstance(date_vals, pd.DatetimeIndex):
            if date_vals.tz is not None:
                date_vals = date_vals.tz_convert("UTC").tz_localize(None)
            # normalize 到午夜（剥掉时分秒），保证与纯日期查询键相等。
            date_vals = pd.DatetimeIndex(date_vals).normalize()
            df = df.set_index(
                pd.MultiIndex.from_arrays(
                    [date_vals, df.index.get_level_values("symbol")],
                    names=df.index.names,
                )
            )
        # Important 1（sort 拷问）：上游同步脚本若未保证索引有序，对未排序索引做
        # .loc[start:end] 切片且边界标签缺失时会抛 KeyError "non-monotonic index"。
        # load() 一次排序，查询路径零开销（sort_index 是 O(n log n) 单次成本，换取后续
        # 所有 slice 的 slice_locs 可正确解析边界）。一次排序、永久受益。
        return df.sort_index()

    @staticmethod
    def _build_ffill_view(df: pd.DataFrame) -> pd.DataFrame:
        """构造价格列已 ffill、量类列保持原值的完整视图（每湖独立）。

        - 仅【价格列】沿时间 ffill：groupby(level="symbol").ffill() 保证不跨标的串味。
        - 拷贝后只改价格列，volume/amount 等保持原始值（停牌日 volume=0，ffill 会造假量）。
        """
        price_cols = [c for c in _PRICE_COLS if c in df.columns]
        if price_cols:
            ffill_view = df.copy()
            ffill_view[price_cols] = df[price_cols].groupby(level="symbol").ffill()
            return ffill_view
        # 无价格列时整体原样返回（策略层自负其责）。
        return df.copy()

    # ---------- 查询 ----------

    def _norm_date(self, date: str, key: str) -> object:
        """把入参日期按指定湖的 date 层级原生 dtype 归一化，规避 str/Timestamp 混比。"""
        dt = self._dtypes.get(key)
        # str 层级（parquet 落盘 str）：原样使用，保持与落盘格式一致。
        if pd.api.types.is_string_dtype(dt):
            return str(date)
        # datetime 层级：转 Timestamp 并 normalize 到午夜（剥掉时分秒）。
        # 兑现注释承诺：与 load() 中已 normalize 的 date 层级键严格对齐，
        # 防止查询键 '2024-01-02 00:00:00' 与 normalize 后的索引键精确相等。
        return pd.Timestamp(date).normalize()

    def _resolve(self, lake: str | None) -> str | None:
        """解析查询的湖 key。无可用湖时返回 None（由调用方判空返空 DF，不 raise）。

        向后兼容红线：brief 原版在无湖时 raise KeyError，会破坏 `test_offline_mode_when_parquet_missing`
        的离线降级契约（无湖 → get_* 返回空 DF，不抛）。这里改成返 None，由 get_* 顶部判空，
        既保留 _default_key 语义又守住离线降级。
        """
        if lake is not None:
            return lake
        # 优先 config 显式 default_lake（如 "daily"），回退首个 load 成功的 key。
        # Why：reader._default_key 按 load 顺序（main.py lifespan 按字典序 macro→sector→daily...），
        # 首个成功的常是 macro，致 get_*(lake=None) 误走宏观湖；显式 default_lake 修正语义。
        configured = LAKE_CONFIG.get("default_lake")
        if configured and configured in self._lakes:
            return configured
        return self._default_key

    def get_cross_section(
        self, date: str, *, lake: str | None = None
    ) -> pd.DataFrame:
        """某日全市场截面（价格取 ffill 版，停牌沿用末次成交价；volume 取原始值不 ffill）。

        无湖或指定湖不存在 → 返回空 DF（离线降级，不抛异常）。
        """
        # 顶部判空：无任何湖或 lake 不在缓存中 → 离线降级返空 DF，守住向后兼容契约。
        key = self._resolve(lake)
        if key is None:
            return pd.DataFrame()
        ff = self._ffills.get(key)
        if ff is None:
            return pd.DataFrame()
        try:
            # xs 取 date 层级；ffill 视图中价格列已补齐末次成交价，volume 为原始值。
            return ff.xs(self._norm_date(date, key), level="date")
        except KeyError:
            logger.warning("截面日期不存在：%s(lake=%s)", date, key)
            return pd.DataFrame()

    def get_timeseries(
        self, symbol: str, start: str, end: str, *, lake: str | None = None
    ) -> pd.DataFrame:
        """单标的原始时序（不 ffill，保留真实停牌空洞供策略层自行决策）。

        无湖或指定湖不存在 → 返回空 DF（离线降级，不抛异常）。
        """
        # 顶部判空：无任何湖或 lake 不在缓存中 → 离线降级返空 DF，守住向后兼容契约。
        key = self._resolve(lake)
        if key is None:
            return pd.DataFrame()
        df = self._lakes.get(key)
        if df is None:
            return pd.DataFrame()
        try:
            ts = df.xs(symbol, level="symbol")
        except KeyError:
            return pd.DataFrame()
        # 闭区间切片；start/end 按 date 层级原生 dtype 归一化后切片。
        return ts.loc[self._norm_date(start, key):self._norm_date(end, key)]
