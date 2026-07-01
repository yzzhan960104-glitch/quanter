"""CreditRegime：日频宏观信贷状态机（+1 扩张 / 0 中性 / -1 收缩）。

它是整个宏观 CTA 体系的【宏观锚】（Epic 2 因子之首），下游消费者：
    - 网关(T14, ExecutionGateway)：收缩态触发【宏观否决】，禁止开新多；
    - 前端(T16, /dashboard)：展示红/黄/绿宏观灯。

信号融合规则（物理意图，三者共振才出方向，单一指标噪声大）：
    +1（扩张）：社融↑（实体融资需求旺盛）+ M1M2 剪刀差为正（资金活化、企业
                活期存款上行）+ DR007 下行（银行间流动性宽松）
    -1（收缩）：社融↓（实体融资萎缩）+ 剪刀差非正（资金沉淀为定期/储蓄）+
                DR007 上行（银行间流动性收紧）
    0（中性）：  其余一切（信号矛盾或样本不足，宁缺毋滥——执行层按中性对待）

无前视红线（量化风控极度拷问）：
    compute(date) 必须用 .loc[:date] 严格时间门控，仅取 date 当日及之前的宏观
    序列。社融/M1M2 是月频数据，sync_macro_credit.align_to_daily 已做"仅向前
    ffill"（用过去值解释现在），本模块只读不再回填——绝不可把 date 之后才
    公布的月度值泄漏给历史日。否则前视偏差会让回测曲线完美、实盘直接崩盘
    （典型未来函数陷阱）。

macro 湖读取约定（关键，与 daily/minute 湖不同）：
    macro 湖（data_lake/macro_credit.parquet，由 Task 5 sync_macro 落盘）是
    【DatetimeIndex，无 symbol 层】——因为宏观指标是全市场级别的，不按标的
    分行存储。故【不能】用 DataLakeReader.get_timeseries(symbol, ...)（它内部
    做 df.xs(symbol, level="symbol")，对无 symbol 层的宏观湖会抛 KeyError）。
    生产侧改用 DataLakeReader._lakes["macro"] 直接拿 df，再 .loc[:date] 切片。
    测试侧通过 __init__(macro_df=...) 注入合成数据，绕开真实 parquet 湖。
"""
from __future__ import annotations

import threading

import pandas as pd


# 判别所需的最小样本数（窗口长度）。20 日 ≈ 1 个月工作日，是判断"趋势"
# 的最小可信样本——短于此，趋势首尾比较的统计噪声会主导信号，强制返 0
# 守护执行层不在小样本误判下做激进动作。
_MIN_LOOKBACK = 20


class CreditRegime:
    """日频宏观信贷状态机：+1 扩张 / 0 中性 / -1 收缩。

    单例 get_default()（双重检查锁）保证网关(T14)与前端(T16)共用同一实例，
    避免 macro 湖被重复加载造成内存翻倍与重复 IO。
    """

    _instance: "CreditRegime | None" = None
    _lock = threading.Lock()

    @classmethod
    def get_default(cls) -> "CreditRegime":
        """双重检查锁单例（仿 data/notifier/DataLakeReader 的 get_instance）。

        第一次判空避免已构造实例每次都进锁（热路径零开销）；
        锁内第二次判空防止并发下两个线程同时通过第一次判空、各自构造实例。
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self, macro_df: pd.DataFrame | None = None) -> None:
        """初始化宏观信贷状态机。

        参数：
            macro_df: 测试注入的合成宏观湖（DatetimeIndex，列含
                      shrzgm/M1M2_gap/dr007）。生产侧传 None，由 _load_from_lake()
                      在首次 compute 时惰性从 DataLakeReader macro 湖载入。
        """
        # 惰性载入标记：仅当 _macro 为 None 且首次 compute 时才打 macro 湖，
        # 避免 import 期即触发 IO（离线开发机/CI 无湖也能 import 本模块）。
        self._macro: pd.DataFrame | None = macro_df

    # ----------------------------------------------------------
    # macro 湖读取
    # ----------------------------------------------------------

    def _load_from_lake(self) -> pd.DataFrame:
        """从 DataLakeReader macro 湖载入完整宏观序列。

        ⚠️ macro 湖无 symbol 层：DataLakeReader 的常规 get_timeseries(symbol,...)
        会 df.xs(symbol, level="symbol")，对宏观湖（仅 DatetimeIndex）必然抛
        KeyError。故这里直接取 _lakes["macro"] 的 DatetimeIndex DataFrame，
        不走 symbol 路径。无湖（离线/未 load）时返空 DF，compute 兜底返 0。
        """
        # 延迟 import：避免顶层循环引用（factors <-> data），且测试注入路径
        # 完全不触发此 import，保持单测纯净。
        try:
            from data.lake_reader import DataLakeReader
            reader = DataLakeReader.get_instance()
            # macro 湖是 DatetimeIndex（无 symbol 层），直接取整张表。
            # 注意 _lakes 内是【原始 df，未 ffill】，但 sync_macro_credit 落盘前
            # 已做 ffill，故此处无需二次 ffill；若上游改为不 ffill 落盘，
            # 此处应补 .ffill()——当前按既定契约直接用。
            return reader._lakes.get("macro", pd.DataFrame())
        except Exception:
            # 兜底：任何湖读取异常（import 失败/无湖/键缺失）都返空 DF，
            # 由 compute 的 len<20 分支安全兜底返 0，绝不抛到执行层。
            return pd.DataFrame()

    def _series(self, date: object) -> pd.DataFrame:
        """返回 date 当日及之前的宏观序列（严格时间门控，无前视）。

        ⚠️ 无前视红线：.loc[:pd.Timestamp(date)] 仅取【date 及之前】的行。
        - 社融/M1M2 月频值在 sync_macro_credit 已仅向前 ffill，此处切片只是
          按时间裁剪，不引入未来月度值；
        - 若 macro 湖为空（离线/未载入），惰性从 DataLakeReader 载入一次。
        """
        if self._macro is None:
            self._macro = self._load_from_lake()
        # 切片前必须保证 DatetimeIndex 有序（sync_macro 已 sort_index，但防御性
        # 再排序一次，避免上游异常导致 .loc[:date] 在非单调索引下抛 KeyError）。
        df = self._macro
        if df is None or df.empty:
            return pd.DataFrame()
        # Timestamp 归一化到午夜：与 sync_macro 落盘的 normalize() 索引键对齐，
        # 防止查询键带时分秒导致切片边界不匹配。
        ts = pd.Timestamp(date).normalize()
        try:
            return df.sort_index().loc[:ts]
        except TypeError:
            # 索引 dtype 与 Timestamp 不可比（理论上 sync_macro 落盘已是
            # DatetimeIndex，此处仅做防御性兜底）。
            return pd.DataFrame()

    # ----------------------------------------------------------
    # 核心判别
    # ----------------------------------------------------------

    def compute(self, date: object) -> int:
        """判别 date 当日的宏观信贷状态：+1 扩张 / 0 中性 / -1 收缩。

        算法（极简显式，拒绝黑盒）：
            取 date 之前 _MIN_LOOKBACK（20）日的窗口，比较窗口【首尾】值判趋势：
              - 社融 shrzgm 首尾：上行为扩张、下行为收缩；
              - M1M2_gap 末值：>0 为资金活化、否则资金沉淀；
              - dr007 首尾：下行为宽松、上行为收紧。
            三者共振（同向）才出 +1 / -1，否则 0（中性）。

        无前视：仅用 _series(date) 的 .loc[:date] 切片，date 之后数据不可见。
        """
        s = self._series(date)
        # 样本不足或缺失列 → 安全返 0（小样本趋势误判防御）。
        if s.empty or len(s) < _MIN_LOOKBACK:
            return 0
        win = s.tail(_MIN_LOOKBACK)

        # 缺列防御：shrzgm + M1M2_gap 是核心信号（必须有）；
        # dr007 可选——AKShare 暂无干净的 DR007 接口（repo_rate_hist 停更于 2020），
        # 缺失时不参与否决（rate_down/rate_up 视作 True），用双信号判别。
        core = ("shrzgm", "M1M2_gap")
        if any(c not in win.columns for c in core):
            return 0
        has_dr = "dr007" in win.columns

        # 窗口首尾比较判趋势（向量化切片访问，无 for 循环热点）。
        credit_up = win["shrzgm"].iloc[-1] > win["shrzgm"].iloc[0]
        gap_pos = win["M1M2_gap"].iloc[-1] > 0
        rate_down = (win["dr007"].iloc[-1] < win["dr007"].iloc[0]) if has_dr else True
        if credit_up and gap_pos and rate_down:
            return 1

        credit_down = win["shrzgm"].iloc[-1] < win["shrzgm"].iloc[0]
        rate_up = (win["dr007"].iloc[-1] > win["dr007"].iloc[0]) if has_dr else True
        if credit_down and (not gap_pos) and rate_up:
            return -1

        return 0

    # ----------------------------------------------------------
    # 可选：近 N 日状态序列（T16 前端驾驶舱历史图会用）
    # ----------------------------------------------------------

    def history(self, n: int = 30) -> pd.Series:
        """返回近 N 日的逐日状态序列（index=date, values∈{+1,0,-1}）。

        用途：T16 /dashboard 端点绘制宏观状态历史迁移曲线（红黄绿带）。
        实现：在已载入的 macro 湖尾部取 n 个工作日，逐日复用 compute(date)
        保证无前视语义一致。O(n) 单次扫描，n≤60 通常 <10ms，无需向量化。
        """
        if self._macro is None:
            self._macro = self._load_from_lake()
        df = self._macro
        if df is None or df.empty:
            return pd.Series(dtype=int)
        tail = df.sort_index().tail(n)
        # 逐日 compute：复用同一无前视判别路径，语义与 compute(date) 严格一致。
        return pd.Series(
            {d: self.compute(d) for d in tail.index},
            dtype=int,
        ).sort_index()
