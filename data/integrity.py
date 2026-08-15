# -*- coding: utf-8 -*-
"""数据完整性核心：停牌区间重建 + 交易日基准（阶段 1 基础组件）。

物理意图：lake 数据有完整性缺口——标的停牌复牌段漏采，导致颈线法等识别用残缺数据
误判（300214.SZ 案例：缺 07-14~07-21 → 颈线 8.07 → 07-24 close 11.86 误判突破）。
本模块提供「区分合法跳空（停牌）vs 漏采」的基础设施，供规则 1/3/4 复用。

停牌语义（A 股物理事实，算法必须遵守）：
- S（停牌）当日：标的停牌，无行情 → 算停牌日（lake 缺这根 = 合法跳空）
- R（复牌）当日：标的恢复交易，有行情 → 不算停牌日（lake 应有这根，缺 = 漏采）
- 停牌区间 = [S 日, R 日) 之间的交易日（含 S 不含 R）
- 未复牌（S 后无 R）：从 S 到最新交易日都算停牌

依赖注入红线：load_suspend_intervals 接收 suspend_df + trade_days_set 作入参（纯函数，
不读文件/不触网），便于单测；文件读取与 token 获取由调用方（find_gaps / scan CLI）负责。
"""
from __future__ import annotations

import logging
import os
import statistics
from dataclasses import dataclass
from typing import Set

import pandas as pd
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


# ============================================================================
# 结果数据结构（frozen，仿 data/freshness.py 的 FreshnessResult，便于聚合与断言）
# ============================================================================

@dataclass(frozen=True)
class GapRange:
    """一段缺口（规则 1 扫描输出 / 规则 3 补采输入）。

    suspend_justified=True 表示整段是停牌（合法跳空，无需补采）；False 表示漏采（需补）。

    N1 停牌真值（2026-08-16）日级化升级——旧段级 ``all(d in susp)`` 的放大效应：
    段内任一日缺 S 事件 → 整段误判漏采（000670.SZ 589 天洞仅 11 行 S → 578 个
    无记录日拖累整段）。新字段把「该补哪些日」精确到日：
    - unjustified_days：日级判定后真需补采的交易日（suspend_d 解释 + 长洞市场
      共识启发式排除后的剩余）；repair 以此为单位（真缺一日补一日）。
    - suspend_suspected：整段由市场共识启发式判停牌（非 suspend_d 铁证）——
      justified-with-flag，scan 报告单列计数，与铁证停牌区分（可审计可追溯）。
    """
    symbol: str
    start: str                            # 缺口起 YYYY-MM-DD
    end: str                              # 缺口止 YYYY-MM-DD
    missing_dates: tuple[str, ...]        # 该段缺失的交易日（不可变，可哈希）
    suspend_justified: bool
    unjustified_days: tuple[str, ...] = ()   # 真需补的日（缺省见 __post_init__ 兼容填充）
    suspend_suspected: bool = False          # 市场共识推定停牌（长洞启发式命中）

    def __post_init__(self):
        # 旧构造兼容：旧报告 JSON / 既有调用方只传 5 字段——suspend_justified=False
        # 的段在旧段级语义下「全段皆漏」，隐含 unjustified_days = 全部 missing_dates
        # （等价旧行为，repair 不空转）；justified 段恒空。frozen 需绕 __setattr__ 写。
        if self.unjustified_days == () and self.suspend_justified is False:
            object.__setattr__(self, "unjustified_days", tuple(self.missing_dates))


@dataclass(frozen=True)
class ContinuityResult:
    """窗口连续性检查结果（规则 4 scan_live gate 用）。

    ok=True 表示窗口完整或仅含停牌跳空；False 表示含未解释漏采（gate 据此跳过标的）。
    """
    ok: bool
    missing_dates: tuple[str, ...]        # 窗口内所有缺失交易日
    unjustified: tuple[str, ...]          # 缺失且非停牌 = 漏采（gate 判定依据）


# ============================================================================
# 规则 2：suspend_d S/R 事件 → per-symbol 停牌交易日集合
# ============================================================================

def load_suspend_intervals(
    suspend_df: pd.DataFrame, trade_days_set: Set[str]
) -> dict[str, Set[str]]:
    """suspend_d 的 S/R 事件 → per-symbol 停牌交易日集合。

    Args:
        suspend_df: suspend_d parquet 内容（MultiIndex(date, symbol) + suspend_type 列，
                    suspend_type ∈ {"S","R"}，suspend_timing 字段不可靠忽略）。
        trade_days_set: 全期交易日集合（YYYY-MM-DD），用于把 S/R 区间展开为交易日
                        （S/R 之间的非交易日不算停牌——本就无行情）。

    Returns:
        {symbol: set(停牌交易日 YYYY-MM-DD)}。无停牌记录的标的不在 dict
        （调用方 .get(sym, set()) 取空集，语义=该标的从未停牌）。
    """
    intervals: dict[str, Set[str]] = {}
    # per-symbol 独立重建（groupby 保证不跨标的串味）
    for sym, grp in suspend_df.groupby(level="symbol"):
        events = (grp.reset_index()
                  .sort_values("date")[["date", "suspend_type"]])
        sym_days: Set[str] = set()
        pending_S: str | None = None
        # 逐事件扫描：S 起区间、R 终区间
        for _, row in events.iterrows():
            dstr = pd.Timestamp(row["date"]).strftime("%Y-%m-%d")
            t = row["suspend_type"]
            if t == "S":
                pending_S = dstr
                sym_days.add(dstr)            # S 当日停牌（无行情）
            elif t == "R":
                if pending_S is not None:
                    # [pending_S, R) 之间的交易日加入（含 S 已加，补中间交易日；R 不含=复牌有行情）
                    for td in trade_days_set:
                        if pending_S <= td < dstr:
                            sym_days.add(td)
                pending_S = None
        # 末尾未复牌（S 后无 R）：从 S 到最大交易日都算停牌
        if pending_S is not None:
            for td in trade_days_set:
                if td >= pending_S:
                    sym_days.add(td)
        if sym_days:
            intervals[sym] = sym_days
    return intervals


# ============================================================================
# 交易日基准：trade_cal → 区间交易日集合
# ============================================================================

def fetch_trade_days(start: str, end: str) -> Set[str]:
    """[start, end] 闭区间的交易日集合（YYYY-MM-DD）。

    逐年 _fetch_year_trade_cal 合并（跨年区间需多年），过滤到 [start, end]。
    边界含 start/end（与 DataLakeReader.get_timeseries 的 .loc[start:end] 闭区间同口径）。
    """
    sy, ey = int(start[:4]), int(end[:4])
    days: Set[str] = set()
    for y in range(sy, ey + 1):
        for d in _fetch_year_trade_cal(y):
            if start <= d <= end:
                days.add(d)
    return days


def _fetch_year_trade_cal(year: int) -> list[str]:
    """封装 data/calendar.fetch_trade_cal（便于测试 monkeypatch，避免触网）。

    M1 循环切断（2026-08-12）：fetch_trade_cal 已下沉到 data/calendar.py，本函数
    改从 data 层取（data→data），不再反查 trading.calendar——断 data.integrity→
    trading.calendar 真函数级循环。

    返回该年交易日列表（YYYY-MM-DD）。无 token/网络时 calendar 内部 weekday 兜底
    （仅识周末不识节假日——上层应据告警排查）。
    """
    from data.calendar import fetch_trade_cal
    return fetch_trade_cal(year)


# ============================================================================
# 写入前历史行数守卫（T13-A · L1 防抹除）
# ============================================================================
# 物理意图（T12 实证）：通用同步器 to_parquet 直接覆盖，无写入前守卫 →
# a_shares_daily 被 sync_tushare.py daily 残片覆盖（1020万→3200）。所有湖写入口
# （sync/repair/结果回湖）落盘前必须经本守卫：新行数相对现有骤降 → 拒写 + CRITICAL。
WRITE_GUARD_MIN_RATIO = 0.9   # 新行数 < 现有 × 0.9 → 视为骤降，拒写


class WriteGuardError(RuntimeError):
    """写入守卫拒绝：新行数相对现有骤降，疑为残片覆盖/部分回采，拒写保护历史。"""


def existing_row_count(path: str) -> int | None:
    """读 parquet 行数（pyarrow 元数据，不读数据体，免 454MB 全量 IO）。

    Returns:
        行数（文件存在且合法）；None（文件不存在或损坏——调用方据此区分「无基线」
        vs「基线不可读」）。
    """
    if not os.path.exists(path):
        return None
    try:
        return pq.read_metadata(path).num_rows
    except Exception:
        # 损坏/非 parquet：返 None，由 assert_safe_overwrite 决策（默认拒写，不静默）
        logger.warning("读 parquet 行数失败（损坏？）：%s", path, exc_info=True)
        return None


def check_row_count_drop(baseline: int, new: int,
                         min_ratio: float = WRITE_GUARD_MIN_RATIO) -> tuple[bool, str]:
    """行数骤降判定（SSoT 纯函数）：new < baseline × min_ratio → 骤降。

    freshness 行数骤降维度与写入守卫共用本函数（蓝图 §5 原则 2，禁止两套实现）。

    Args:
        baseline: 基线行数（写入守卫=现有文件行数；freshness=上次健康检查行数）。
        new:      待判定行数。
        min_ratio: 放行下限比（默认 0.9）。

    Returns:
        (ok, reason)：ok=True 放行；ok=False 骤降，reason 含中文结论供日志/断言。
    """
    if baseline <= 0:
        return True, "基线为 0/无历史，无骤降可言，放行"
    if new >= baseline * min_ratio:
        return True, f"new={new} >= baseline×{min_ratio}={int(baseline*min_ratio)}，放行"
    return False, (f"行数骤降：new={new} < baseline×{min_ratio}={int(baseline*min_ratio)}"
                   f"（baseline={baseline}），疑为残片覆盖/部分回采")


def assert_safe_overwrite(lake_path: str, new_df: pd.DataFrame, *,
                          min_ratio: float = WRITE_GUARD_MIN_RATIO,
                          force: bool = False) -> None:
    """写入前历史行数守卫：落盘 to_parquet 前调用，骤降则抛 WriteGuardError 拒写。

    物理意图：封死 T12 式残片覆盖。本次接入的湖写入口：通用同步器 _build_multiindex
    （大湖全量覆盖）、_sync_single（全量覆盖；date_range 窗口中间写除外，由调用方
    sync_incremental [6] old_df 基线守卫兜底）、增量 sync_daily_incremental append、
    repair_gaps 重写。sync_data_lake / sync_macro_credit 等其余写入口未接入（review R3：
    不夸大为「所有湖写入口」）。

    决策矩阵（硬阻断语义，绝不静默）：
        force=True               → 旁路（人为故意缩小重采），CRITICAL 留痕后放行
        现有文件不存在            → 放行（无基线可比，首次写/新湖）
        现有文件损坏/不可读       → 拒写（基线不可读，宁拒不盲写）
        new_df 为空              → 拒写（空写无意义且可能抹除）
        new < baseline × min_ratio → 拒写（骤降，疑残片覆盖/部分回采）
        否则                     → 放行

    Args:
        lake_path: 落盘路径（读现有行数用）。
        new_df:    待写的 DataFrame（取 len 比对）。
        min_ratio: 骤降下限比（默认 0.9）。
        force:     逃生口（配合 QUANTER_FORCE_WRITE=1，调用方传入）。

    Raises:
        WriteGuardError: 拒写时抛，调用方应让其传播（阻断本次落盘）。
    """
    if force:
        # 逃生口留痕：守卫可拒绝、可强旁、不可静默
        logger.critical("FORCE 写入 %s（已旁路行数守卫，人为操作留痕）", lake_path)
        return
    existing = existing_row_count(lake_path)
    if existing is None and not os.path.exists(lake_path):
        return  # 首次写/新湖：无基线，放行
    new_len = len(new_df)
    if existing is None:
        # 文件存在但读不出（损坏）→ 拒写，不静默
        raise WriteGuardError(
            f"{lake_path} 现有文件损坏/行数不可读，拒写（基线不可信，宁拒不盲写）")
    if new_len == 0:
        raise WriteGuardError(f"{lake_path} 待写为空 df，拒写（空写无意义且可能抹除）")
    ok, reason = check_row_count_drop(existing, new_len, min_ratio)
    if not ok:
        logger.critical("写入守卫拒写 %s：%s", lake_path, reason)
        raise WriteGuardError(f"{lake_path} {reason}")


def _fsync_path(path: str) -> None:
    """fsync 文件刷盘（原子写入最佳实践，防 OS page cache 断电丢数据）。

    物理意图：``to_parquet`` 写完后数据可能仅落 OS page cache（未刷磁盘）——正常关机时
    内核会刷盘，但断电/内核 panic 极端场景下 page cache 内半截数据会丢。``fsync`` 强制
    把文件数据 + 元数据刷到磁盘存储层，确保 ``tmp`` 文件完整落盘后再 ``os.replace``——
    达到「真原子」（rename 原子 + 内容也已持久化）。

    容错：fsync 失败（权限/网络盘不支持/文件已被关）只 log warning 不抛——``os.replace``
    仍执行。理由：fsync 失败属于「断电极端场景退化」，绝大多数情况 page cache 仍会被
    内核异步刷盘，原子 rename 仍成立；抛错反而会让可用性退化（写入失败但实际数据已写完）。
    """
    try:
        # Windows 平台正确性：FlushFileBuffers 需写权限，"rb" 只读句柄在 Windows 上 fsync
        # 大概率恒失败→落 except 静默降级（即 docstring 声明的容错）。改 "r+b"（读写、不截断），
        # 让原子写的 fsync 在目标平台真生效——「断电不丢」承诺不再因只读句柄静默退化。
        with open(path, "r+b") as f:
            os.fsync(f.fileno())
    except OSError:
        logger.warning("fsync 失败（文件系统不支持？正常关机仍会刷盘，原子 rename 仍成立）：%s",
                       path, exc_info=True)


def atomic_write_parquet(lake_path: str, new_df: pd.DataFrame) -> None:
    """原子落盘 parquet（tmp + fsync + os.replace 同卷原子），不含行数守卫。

    物理意图（G5 数据原子写）：把原子写入逻辑从 safe_overwrite 抽出为独立工具，供两类
    调用方复用——
      - ``safe_overwrite``：守卫 + 原子写（绝大多数湖写入口，需守卫防残片覆盖）；
      - ``date_range`` 旁路守卫场景（tushare_sync._sync_single 窗口中间态写，行数本来就
        比现有小，守卫会误拒；但仍需原子写防半截损坏）。
    DRY 红线：原子写逻辑（tmp + fsync + os.replace + 异常清理）只此一处实现。

    同卷保证：tmp = ``path + ".<pid>.tmp"`` 与 path 同目录（默认）→ 同卷 → ``os.replace``
    原子（跨卷 rename 非原子，会先 copy 再 unlink，中途异常留半截）。PID 防御并发：多进程
    并发写同 path 时 tmp 不撞（虽然本项目生产串行，PID 防御性）。

    异常清理：to_parquet/fsync/replace 任一失败时清理 tmp（防遗留 .tmp 干扰下次写），
    原文件不动——这是原子写的核心收益（异常不损原文件）。
    """
    tmp = f"{lake_path}.{os.getpid()}.tmp"
    try:
        new_df.to_parquet(tmp, engine="pyarrow")
        _fsync_path(tmp)  # 刷盘（断电极端场景兜底，正常关机内核也会刷）
        os.replace(tmp, lake_path)  # 同卷原子 rename（POSIX rename(2)/Win MoveFileEx 原子）
    except Exception:
        # 异常清理 tmp：防遗留 .tmp 干扰下次写（ls 见 .tmp 误判残留态）。
        # 原文件未动——to_parquet/fsync/replace 失败时，target 完整保留旧内容（原子核心）。
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                logger.warning("atomic_write_parquet 清理 tmp 失败（残留不影响正确性）：%s",
                               tmp, exc_info=True)
        raise


def safe_overwrite(lake_path: str, new_df: pd.DataFrame, *,
                   min_ratio: float = WRITE_GUARD_MIN_RATIO) -> None:
    """写入前历史行数守卫 + 原子落盘（assert_safe_overwrite 守卫 + atomic_write_parquet 原子写）。

    物理意图（G5 数据原子写 · 治半截损坏 parquet）：
        原实现 safe_overwrite 只做行数守卫，落盘由调用方紧跟的 ``df.to_parquet(path)``
        直写目标——写入中途 OOM/断电/磁盘满会留半截损坏 parquet，下次 ``read_parquet``
        抛 EOFError 读不全，需全量回采（生产湖 1020 万行 × 5000 标的）才能恢复。本函数
        把「守卫 + 原子写」收口为单一入口：守卫通过后调 atomic_write_parquet（tmp + fsync
        + os.replace 同卷原子）。异常（守卫拒写 / 写入失败）时原文件不动。

    签名向后兼容：原 safe_overwrite ``只验不写``、调用方紧跟 ``to_parquet``；现在 safe_overwrite
    完成原子写入，所有 5 处调用方（tushare_sync / sync_macro_credit / sync_data_lake /
    sync_daily_incremental / repair_gaps）已同步移除紧跟的 ``to_parquet``，落盘收口到此单点。
    """
    # ① 守卫（骤降拒写，硬阻断语义不变；force=QUANTER_FORCE_WRITE=1 逃生口）
    assert_safe_overwrite(lake_path, new_df, min_ratio=min_ratio,
                          force=os.environ.get("QUANTER_FORCE_WRITE") == "1")
    # ② 原子写入（tmp + fsync + os.replace，异常时清理 tmp、原文件不动）
    atomic_write_parquet(lake_path, new_df)


# ============================================================================
# 规则 1：全市场连续性扫描
# ============================================================================

# ============================================================================
# N1 长洞市场共识启发式（2026-08-16 勘定根因的修复侧）
# ============================================================================
# 物理意图：suspend_d 2019 年后对长停牌只记零星 S（000670.SZ 589 天洞仅 11 行、
# 000792.SZ 311 天 13 行、000995.SZ 399 天 8 行）——停牌 ground-truth 本身残缺，
# 严格按 suspend_d 判会把大量真停牌长洞误报为漏采（实跑 16,371 段 unjustified 的
# 主因）。市场共识是独立于 suspend_d 的第二真值源：洞窗内每日湖在场数健康
# （≥ 窗口中位数 × 0.8，即湖自身无全市场残缺日）而唯独该标的缺席 + 标的段前后
# 均有行情 → 物理上唯一自洽的解释是停牌（000670.SZ 洞窗实证：每日在场
# 3,662-4,684 标的，零日低于中位数 80%）。
#
# 风险权衡（两方向误判代价不对称，参数从严的原因）：
# - 误放真缺（把漏采判停牌）→ 数据永缺：repair 永不补、scan 永不报——不可逆。
# - 误补停牌（把停牌判漏采）→ repair 拉空一轮：浪费配额/限频预算，下轮 unfillable
#   sidecar 标记后收敛——可逆、可观测。
# 故三重前置全部满足才推定：段长 ≥10 交易日（短段 suspend_d 2017-2018 密集覆盖
# 期真短漏概率高，从严）+ 每日在场数 ≥ 中位数×0.8（湖故障日不背书）+ 标的段前后
# 均有数据（退市末段/上市初段不适用）。
#
# 2017 前边界：湖仅 ~20 标的在场面（逐年中位数 ≤24 vs 2017+ ≥2,908）——「市场
# 共识」分母残缺，20 vs 16 的波动无统计意义。用在场数下限（而非硬编码年份）闸：
# 中位数 < 下限 → 启发式不适用，退回严格 suspend_d 判定（宁从严）。
SUSPEND_SUSPECT_MIN_DAYS = 10            # 推定停牌的最小段长（交易日）
SUSPEND_SUSPECT_PRESENCE_RATIO = 0.8     # 每日在场数 ≥ 窗口中位数 × 该比的下限
SUSPEND_SUSPECT_MIN_MARKET_PRESENCE = 1000  # 窗口中位数在场数下限（2017 前分母残缺闸）


def _is_suspend_suspected(
    seg: list[str], market_presence: dict[str, int], *,
    has_data_before: bool, has_data_after: bool,
) -> bool:
    """长洞市场共识启发式内核：三重前置全满足才推定停牌（纯函数，可直测）。

    Args:
        seg: 连续缺失交易日段（find_gaps 的一个 gap 段，时间序）。
        market_presence: {YYYY-MM-DD: 湖该日在场标的数}（scan 从湖日级计数喂入；
                    缺日的在场数按 0 计——湖连在场计数都没有的日不可为其背书）。
        has_data_before/has_data_after: 标的在该段前/后是否均有行情（洞须夹在
                    真实行情之间；find_gaps 的 [amin,amax] 边界结构上保证为 True，
                    此处显式校验防未来重构破坏边界语义）。
    """
    if len(seg) < SUSPEND_SUSPECT_MIN_DAYS:
        return False
    if not (has_data_before and has_data_after):
        return False
    counts = [market_presence.get(d, 0) for d in seg]
    median = statistics.median(counts)
    # 2017 前分母残缺闸：窗口中位数不足下限 → 市场共识无统计意义，不推定
    if median < SUSPEND_SUSPECT_MIN_MARKET_PRESENCE:
        return False
    # 每日健康闸：任一日湖在场数 < 中位数×0.8 → 该日湖自身可疑，不能为个股缺席背书
    return all(c >= median * SUSPEND_SUSPECT_PRESENCE_RATIO for c in counts)


def unjustified_subsegments(gap: GapRange) -> list[tuple[str, ...]]:
    """GapRange 的 unjustified_days 按段内连续性拆子段（repair 的原子单位）。

    以 missing_dates（段日序骨架）遍历：连续的 unjustified 日合并为一个子段，
    中间隔着被解释日（suspend_d/启发式）即断开——物理上是两处独立漏采，
    recency-first 配额与 unfillable 标记都以子段为粒度。
    """
    keep = set(gap.unjustified_days)
    runs: list[tuple[str, ...]] = []
    cur: list[str] = []
    for d in gap.missing_dates:
        if d in keep:
            cur.append(d)
        elif cur:
            runs.append(tuple(cur))
            cur = []
    if cur:
        runs.append(tuple(cur))
    return runs


def find_gaps(
    df_lake: pd.DataFrame, trade_days_set: Set[str],
    suspend_intervals: dict[str, Set[str]],
    market_presence: dict[str, int] | None = None,
) -> list[GapRange]:
    """全市场连续性扫描：找出每个标的在 [首日, 末日] 区间内「应有却缺失」的交易日段。

    算法（实测全市场 ~1.2s，groupby level=symbol 一次扫）：
        per-symbol: expected = trade_days ∩ [actual_min, actual_max]
                    沿 trade_days 顺序遍历，连续的 missing（非 actual）合并成一段
    区间边界用 [actual_min, actual_max]：标的上市前/退市后的日期不要求（避免误报）。

    N1 日级判定（2026-08-16）：段内不再段级 all() 一刀切——每日独立判定
    「suspend_d 解释与否」，输出 unjustified_days（真需补日集）；长洞（≥10 日）
    另走市场共识启发式（market_presence 喂入时）判 suspend_suspected。

    Args:
        df_lake: daily 湖（MultiIndex(date, symbol) + OHLCV 列）。
        trade_days_set: 全期交易日集合（fetch_trade_days 输出）。
        suspend_intervals: load_suspend_intervals 输出（{symbol: 停牌日集合}）。
        market_presence: {YYYY-MM-DD: 湖该日在场标的数}——长洞市场共识启发式的
                    分母（scan 全市场路径从湖自身计数喂入，~1.5s/10.3M 行）；
                    None=不启用启发式（既有调用方 _backscan_recent 兼容，行为同旧版）。

    Returns:
        list[GapRange]：每个 GapRange 是一段连续缺口；suspend_justified=True 表示
        全段合法跳空（suspend_d 铁证或启发式推定，后者带 suspend_suspected=True），
        False 表示含真缺日（unjustified_days 列出，repair 以子段为单位补）。
    """
    gaps: list[GapRange] = []
    # groupby(sort=False) 不额外排序（lake 已 sort_index），groupby 键即唯一 symbol。
    for sym, grp in df_lake.groupby(level="symbol", sort=False):
        dates_idx = grp.index.get_level_values("date")
        actual = {pd.Timestamp(d).strftime("%Y-%m-%d") for d in dates_idx}
        if not actual:
            continue
        amin, amax = min(actual), max(actual)
        # expected = 该标的首末交易日之间的所有交易日（sorted 保证遍历顺序 = 时间顺序）
        expected_sorted = sorted(d for d in trade_days_set if amin <= d <= amax)
        seg: list[str] = []
        for d in expected_sorted:
            if d in actual:
                if seg:  # 遇到 actual → 连续 missing 段结束
                    gaps.append(_build_gap_range(sym, seg, suspend_intervals,
                                                 market_presence, amin, amax))
                    seg = []
            else:
                seg.append(d)
        if seg:  # 末尾缺口段
            gaps.append(_build_gap_range(sym, seg, suspend_intervals,
                                         market_presence, amin, amax))
    return gaps


def _build_gap_range(
    symbol: str, seg: list[str], suspend_intervals: dict[str, Set[str]],
    market_presence: dict[str, int] | None = None,
    amin: str | None = None, amax: str | None = None,
) -> GapRange:
    """把连续 missing 段组装成 GapRange（N1 日级判定 + 长洞市场共识启发式）。

    判定序：① 日级 suspend_d 解释 → unjustified 日集；② 非空且喂了在场计数 →
    长洞启发式可整体推定停牌（suspend_suspected=True，unjustified 清空）；
    ③ 输出 suspend_justified = unjustified 日集是否为空。
    """
    susp = suspend_intervals.get(symbol, set())
    # ① 日级判定：段内逐日问 suspend_d（替代旧段级 all()——零星 S 不再拖累整段）
    unjustified = [d for d in seg if d not in susp]
    suspend_suspected = False
    if unjustified and market_presence is not None:
        # ② 长洞市场共识启发式：前置见 _is_suspend_suspected 注释（两方向风险权衡）
        # has_data_before/after：段严格夹在标的 amin/amax 行情之间（结构上恒真，
        # ISO 日期串比较；显式校验防 [amin,amax] 边界语义被未来重构破坏）
        if _is_suspend_suspected(
                seg, market_presence,
                has_data_before=(amin is not None and seg[0] > amin),
                has_data_after=(amax is not None and seg[-1] < amax)):
            unjustified = []
            suspend_suspected = True
    return GapRange(
        symbol=symbol, start=seg[0], end=seg[-1],
        missing_dates=tuple(seg),
        suspend_justified=(not unjustified),
        unjustified_days=tuple(unjustified),
        suspend_suspected=suspend_suspected,
    )


# ============================================================================
# 规则 4：窗口连续性检查（scan_live gate 用）
# ============================================================================

def check_window_continuity(
    df_window: pd.DataFrame, trade_days_set: Set[str],
    suspend_intervals: dict[str, Set[str]], symbol: str,
) -> ContinuityResult:
    """窗口连续性检查：判断 df_window（单标的 OHLCV）是否含未解释漏采。

    gate 判定逻辑：
        expected = trade_days ∩ [窗口首日, 窗口末日]
        missing = expected - 窗口实际索引
        unjustified = missing - suspend_intervals[symbol]   （漏采 = 非停牌的缺失）
        ok = (unjustified 为空)

    ok=True 表示窗口完整或仅含停牌跳空（合法）；ok=False 表示含漏采，gate 应跳过该标的。

    Args:
        df_window: 单标的 OHLCV（DatetimeIndex），通常是 df_upto.tail(window)。
        trade_days_set: 全期交易日集合。
        suspend_intervals: load_suspend_intervals 输出。
        symbol: 当前标的（查停牌区间用）。
    """
    actual = {pd.Timestamp(d).strftime("%Y-%m-%d") for d in df_window.index}
    if not actual:
        # 空窗口不拦（detect 内部 len(df)<window 检查会处理短窗口）
        return ContinuityResult(ok=True, missing_dates=(), unjustified=())
    wmin, wmax = min(actual), max(actual)
    expected = {d for d in trade_days_set if wmin <= d <= wmax}
    missing = sorted(expected - actual)
    if not missing:
        return ContinuityResult(ok=True, missing_dates=(), unjustified=())
    susp = suspend_intervals.get(symbol, set())
    unjustified = tuple(d for d in missing if d not in susp)
    return ContinuityResult(
        ok=(len(unjustified) == 0),
        missing_dates=tuple(missing),
        unjustified=unjustified,
    )


# ============================================================================
# 规则 4 · universe 级：filter_universe_by_continuity（Task 7 U5 gate 下沉）
# ============================================================================
# 物理意图（300214.SZ 漏采教训 · memory data-lake-integrity-gap）：
#   原完整性 gate 内联在 strategies/neckline_method.scan_live（per-symbol 自验窗口连续性），
#   导致「策略层混入数据质量代码」+ 「回测/实盘各走各的 gate」。Task 7 把 gate 上提到
#   data/integrity 的 universe 级纯函数：调用方（trading/engine._eod / backtest/replay.replay）
#   先 filter universe，策略层 scan_live 假设已过滤——回测/实盘共用同一 filter（数据校验单源）。
#
# strangler 红线：本函数逻辑零改动于 scan_live 原内联 gate（同调 check_window_continuity，
# 只从 per-symbol 上提到 universe 级 pre-filter）。等价性依据：
#   - scan_live:228-235 原逻辑 = 取 df_upto.tail(window) → check_window_continuity →
#     not ok 则 return []（该 symbol 不产信号）
#   - 本函数 = 遍历 universe，对 df_map[sym].tail(window) 调 check_window_continuity，
#     not ok 则不含入 clean_universe（该 symbol 不进 scan_live）→ 信号等价

def filter_universe_by_continuity(
    universe, df_map, window, susp, trade_days,
):
    """universe 级完整性 gate：过滤窗口含未解释漏采的 symbol。

    遍历 ``universe``，对每个 symbol 的 ``df_map[sym].tail(window)`` 调
    ``check_window_continuity``，不通过的（窗口含未解释漏采）从 clean_universe 中过滤。
    与原 scan_live 内联 gate 等价（同一 check_window_continuity 逻辑，只上提到 universe 级）。

    物理意图（300214.SZ 教训）：lake 缺停牌复牌段时残缺数据误判颈线突破产误信号；
    gate 在 scan_live 前置过滤掉漏采 symbol，让策略层假设 df_upto 已完整——策略层零数据代码。

    Args:
        universe: 待过滤的 symbol 列表（str 序列）。
        df_map: {symbol: df_upto}（每标的截至当日的 OHLCV，DatetimeIndex）。
                缺失某 symbol 或对应值为 None → 该 symbol 被过滤（不抛错）。
        window: 识别窗口长度（颈线法 id_cfg["window"]，与 scan_live 同口径）。
        susp: load_suspend_intervals 输出（{symbol: 停牌日集合}），区分合法跳空 vs 漏采。
        trade_days: 全期交易日集合（fetch_trade_days 输出）。

    Returns:
        clean_universe: list[str] —— 通过完整性 gate 的 symbol，**保持 universe 输入顺序**。

    fail-open 红线（与原 scan_live:229 `if _td:` 同口径）：
        trade_days 为空集（加载失败/测试降级）时，check_window_continuity 的 expected 恒为
        空集 → missing 恒空 → ok=True，全放行（退回原行为，不阻断识别）。
    """
    import logging
    _log = logging.getLogger("data.integrity")
    clean: list = []
    for sym in universe:
        df = df_map.get(sym) if hasattr(df_map, "get") else None
        if df is None or len(df) == 0:
            # df_map 缺该 symbol 或空 df（加载失败/历史不足）→ 跳过过滤（不进 clean）。
            # 与 _eod:1206 的 df_upto is None 跳过 scan_live 同口径——不进 clean 即不被扫。
            continue
        # check_window_continuity 复用（strangler 红线：不新建 continuity 判定）。
        # window 对齐 scan_live:232 的 df_upto.tail(self.id_cfg["window"])。
        result = check_window_continuity(df.tail(window), trade_days, susp, sym)
        if result.ok:
            clean.append(sym)
        else:
            # 与 scan_live:234-236 同口径的 warning 留痕（不阻断，只过滤）
            _log.warning(
                "完整性 gate 过滤 %s：窗口含 %d 个未解释漏采交易日 %s（data.integrity.filter_universe_by_continuity）",
                sym, len(result.unjustified), list(result.unjustified[:5]),
            )
    return clean
