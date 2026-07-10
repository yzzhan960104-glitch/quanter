# -*- coding: utf-8 -*-
"""TradePlanGenerator（蔡森形态学流水线 Phase 2 · Task 9）。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本模块是蔡森形态学流水线的"计划生成器"——把 PatternScreener 筛出的候选
    DataFrame 转为结构化的 TradePlan（入场/止损/止盈/盈亏比/有效期/股数），
    供下游执行器（事中风控 + 下单网关）消费。本模块只做"数学计算 + 计划组装"，
    不做任何识别/过滤/下单——每个数字都能追溯到蔡森方法学的明确公式。

核心数学（蔡森 Task 1 精读校准 · 覆盖 plan 旧版倍数语义）：
    ── 颈线满足计算（等额累加，非倍数相乘）──
    来源：docs/caisen-methodology-summary.md §2（鉅統/愛之味案例数值验证）
        H              = 颈线价 − 谷底价        # 颈线绝对高度
        第一波满足      = 颈线价 + H              # = 颈线 + 1×H
        第二波满足      = 第一波满足 + H = 颈线 + 2×H
        第 n 波满足     = 颈线价 + n × H          # 等额累加级数
    关键不变量：take_profit 基于【颈线价】加法累加，不是基于突破价乘以倍数。
    cfg.neckline_height_multiple（默认 2）= 看到第几级满足点（生成到第 n 波）。

    ── C 波低点止损（蔡森实战篇四 p132-133）──
        stop_loss = 谷底价 − stop_loss_atr_buffer × ATR
    谷底价 = W底右底 P3 / 头肩底头底 P4（形态最低点）。ATR 缺省时 buffer 项归零，
    退化为精确谷底止损，亦符合蔡森原著"停损=C波低点"原典（buffer 仅日线噪声保险）。

    ── 盈亏比校验 ──
        rr = (take_profit − entry_upper) / (entry_upper − stop_loss)
        rr < min_rr_ratio(3.0) → 丢弃该计划（25% 胜率下期望值为正的最低 R/R）

    ── 入场回踩区间 ──
        entry_upper = breakout_price              # 突破价（回踩挂单上限）
        entry_lower = breakout_price × (1 − pullback_max_pct)  # 回踩下限

候选 DataFrame 契约（PatternScreener.screen 输出）：
    必填列：symbol, pattern_type, formed_at, breakout_price, neckline_price,
           depth, tension, amount30d, is_valid
    可选列：atr（screener 当前不输出，但 plan.generate 优先读取；缺省时 buffer=0）
    反推补充：bottom_price（screener 不输出）由 neckline/(1+depth) 反推：
        W底 depth = (neckline - bottom) / bottom  →  bottom = neckline / (1 + depth)
        头肩底 depth = (颈线均价 - P4) / P4 ≈ 同构（颈线均价≈breakout 处颈线价近似可接受）

防御性边界（CLAUDE.md 量化风控拷问）：
    - 单候选异常不中断整批生成：try/except 跳过脏行，记录 debug 日志；
    - 除零保护：entry_upper ≤ stop_loss 时跳过（rr 无意义，防 Inf/NaN 污染）；
    - ATR 缺省安全降级：无 ATR → buffer=0 → 精确谷底止损（蔡森原典）；
    - formed_at 非 Timestamp 时强制转换（防 Timestamp 构造抛异常）。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from caisen.config import StrategyConfig
from caisen.risk import RiskManager


# 模块级 logger：单候选异常走 debug 级（不污染 prod 日志，但可调试追溯）
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradePlan:
    """蔡森形态学交易计划（不可变值对象，线程安全）。

    所有字段均为"已计算完成"的快照——下游执行器只读消费，不做二次推导。
    frozen=True 保证计划一旦生成不被篡改（防执行链路中途被修改破坏可审计性）。

    字段物理意图（蔡森方法学对齐）：
        plan_id          : 计划唯一标识（uuid4，便于执行器/日志跨服务关联）；
        symbol           : 标的代码；
        pattern_type     : 触发形态 ∈ {"w_bottom", "head_shoulder"}；
        formed_at        : 形态形成日（pivot 末点所在交易日）；
        breakout_price   : 颈线突破价（回踩挂单上限 entry_upper）；
        neckline_price   : 颈线价（满足计算的加法基准）；
        bottom_price     : 谷底价（C 波低点 = W底右底 P3 / 头肩底头底 P4）；
        H                : 颈线绝对高度 = 颈线价 − 谷底价（满足计算步长）；
        entry_upper      : 回踩挂单上限（= breakout_price）；
        entry_lower      : 回踩挂单下限（= breakout × (1 − pullback_max_pct)）；
        stop_loss        : 止损价（C 波低点 − buffer×ATR，蔡森原典）；
        take_profit      : 第一波满足价 = 颈线价 + 1×H（部分止盈位）；
        take_profit_2x   : 第二波满足价 = 颈线价 + 2×H（主要止盈位）；
        rr_ratio         : 盈亏比 = (take_profit − entry_upper)/(entry_upper − stop_loss)；
        valid_until      : 回踩触发窗口截止日（formed_at + pullback_window_bars 交易日）；
        max_holding_until: 时间止损截止日（formed_at + max_holding_bars 交易日）；
        timeout_exit_threshold : 时间止损砍亏浮盈阈值（持仓超时且浮盈 < 此值则砍亏离场；
                          回测 backtest_replay 与实盘 check_exit 同口径、百分比分母）；
        shares           : 分配股数（A 股整手，position_size 计算）；
        metadata         : 补充元数据（bottom_price 反推来源/atr 来源/原始 depth 等）。
    """
    plan_id: str
    symbol: str
    pattern_type: str
    formed_at: pd.Timestamp
    breakout_price: float
    neckline_price: float
    bottom_price: float
    H: float                    # 颈线绝对高度（满足计算步长）
    entry_upper: float
    entry_lower: float
    stop_loss: float
    take_profit: float          # 第一波满足 = 颈线 + 1×H
    take_profit_2x: float       # 第二波满足 = 颈线 + 2×H
    rr_ratio: float
    valid_until: pd.Timestamp
    max_holding_until: pd.Timestamp
    timeout_exit_threshold: float
    shares: int
    metadata: dict = field(default_factory=dict, hash=False, compare=False)


def generate(
    candidates_df: pd.DataFrame,
    cfg: StrategyConfig,
    risk: RiskManager,
    aum: float,
    date,
    trading_calendar: Optional[pd.DatetimeIndex] = None,
) -> list[TradePlan]:
    """遍历候选 DataFrame，生成盈亏比 ≥ min_rr_ratio 的 TradePlan 列表。

    参数：
        candidates_df: PatternScreener.screen() 输出的候选 DataFrame。
            必填列：symbol/pattern_type/formed_at/breakout_price/neckline_price/
                    depth/tension/amount30d/is_valid
            可选列：atr（缺省时 stop_loss buffer 归零，退化为精确谷底止损）。
        cfg:   蔡森策略全参数模型（满足级数/盈亏比下限/回踩窗口/止损 buffer 等）。
        risk:  事前风控管理器（提供 position_size + macro_position_coef）。
        aum:   账户总资金（position_size 用）。
        date:  当前交易日（macro_position_coef 用，决定仓位系数）。
        trading_calendar: 可选交易日历。提供时按日历推进 valid_until/max_holding_until；
            缺省时用 pd.bdate_range（工作日历，跳周末）兜底——离线/CI 环境无交易日历
            依赖，bdate_range 是最朴素的"跳周末"近似，足够回测/测试使用。

    返回：
        list[TradePlan]，每个计划 rr_ratio ≥ cfg.min_rr_ratio。
        候选为空或全部被盈亏比过滤时返回空列表。

    处理流程（每行候选）：
        1. 谷底价：直接读 row["bottom_price"]（Bug3 废除 neckline/(1+depth) 逆推，
           由形态识别直接给出）；
        2. 等额累加满足计算：H = neckline − bottom；take_profit = neckline + H；
           take_profit_2x = neckline + 2×H（cfg.neckline_height_multiple 默认 2）；
        3. 入场区间：entry_upper = breakout；entry_lower = breakout×(1−pullback_max_pct)；
        4. C 波低点止损：stop_loss = bottom − buffer×ATR（无 ATR 时 buffer=0）；
        5. 盈亏比校验（Bug4）：expected_entry = 回踩区间均价；rr = (第n波满足 − expected_entry)
           / (expected_entry − stop)；rr < min_rr_ratio → 跳过；
        6. 时间窗口：valid_until = formed_at + pullback_window_bars 交易日；
                    max_holding_until = formed_at + max_holding_bars 交易日；
        7. 仓位：shares = risk.position_size(aum, entry_upper, stop_loss, coef)；
        8. 组装 TradePlan（frozen 值对象）。
    """
    plans: list[TradePlan] = []

    # 空候选防御：直接返回空列表（避免对空 DataFrame 迭代触发隐性错误）
    if candidates_df.empty:
        return plans

    # 宏观仓位系数（整批共享一次 compute，避免重复调用 regime 湖）
    coef = risk.macro_position_coef(date)

    for _, row in candidates_df.iterrows():
        # 防御性：单行脏数据不中断整批生成（CLAUDE.md 量化风控·边界审查）
        try:
            plan = _build_plan_from_row(row, cfg, risk, aum, coef, trading_calendar)
        except Exception as exc:
            # 反推/计算异常 → debug 级记录后跳过该行，保证候选批次完整性
            _logger.debug(
                "plan.generate 跳过候选 symbol=%s 异常类型=%s 详情=%s",
                row.get("symbol", "<unknown>"), type(exc).__name__, exc,
            )
            continue
        if plan is not None:
            plans.append(plan)

    return plans


def _build_plan_from_row(
    row: pd.Series,
    cfg: StrategyConfig,
    risk: RiskManager,
    aum: float,
    coef: float,
    trading_calendar: Optional[pd.DatetimeIndex],
) -> Optional[TradePlan]:
    """从候选 DataFrame 单行组装 TradePlan，rr < min_rr_ratio 返回 None。

    本方法是 generate 的内循环主体，独立出来便于单候选调试与异常隔离。
    所有数学步骤与 generate 文档一致，每步显式注释蔡森方法学依据。
    """
    # —— 0. 基本字段提取 + formed_at 强制 Timestamp ——
    symbol = str(row["symbol"])
    pattern_type = str(row["pattern_type"])
    neckline_price = float(row["neckline_price"])
    breakout_price = float(row["breakout_price"])
    depth = float(row["depth"])
    formed_at = pd.Timestamp(row["formed_at"])

    # —— 1. 谷底价：直接读取形态识别结果（Bug3 废除 neckline/(1+depth) 逆推）——
    # 旧逆推公式依赖 depth 精度 + neckline 几何，极度脆弱，且受 Bug2 颈线错误连锁影响
    # （neckline 错 → bottom 逆推错 → H/止盈/止损全错）。现由 w_bottom/head_shoulder
    # detect 直接给出 bottom_price（W底=min(p1,p3)，头肩底=p4），契约更稳健。
    if "bottom_price" not in row or pd.isna(row.get("bottom_price")):
        return None   # 候选缺谷底价（契约不完整），跳过
    bottom_price = float(row["bottom_price"])
    if bottom_price <= 0 or depth <= 0:
        return None   # 脏数据防御（非正谷底价 / 非正 depth）

    # —— 2. 颈线满足计算（等额累加，蔡森 §2）——
    # H = 颈线价 − 谷底价；take_profit = 颈线 + 1×H；take_profit_2x = 颈线 + 2×H
    # 关键：基于【颈线价】加法，不是基于突破价乘以倍数（Task 1 校准覆盖 plan 旧版）。
    H = neckline_price - bottom_price
    take_profit = neckline_price + 1.0 * H      # 第一波满足
    # 多级满足：cfg.neckline_height_multiple 控制看到第几级（默认 2 = 第二波满足）
    n = cfg.neckline_height_multiple
    take_profit_n = neckline_price + n * H       # 第 n 波满足（生成到第 n 级）
    take_profit_2x = neckline_price + 2.0 * H    # 第二波满足（固定暴露，便于执行器分级止盈）

    # —— 3. 入场回踩区间 ——
    # entry_upper = 突破价（回踩挂单上限）；entry_lower = 突破价 × (1 − pullback_max_pct)
    entry_upper = breakout_price
    entry_lower = breakout_price * (1.0 - cfg.pullback_max_pct)

    # —— 4. C 波低点止损（蔡森实战篇四 p132-133）——
    # stop_loss = bottom_price − stop_loss_atr_buffer × ATR
    # ATR 来源优先级：候选 DataFrame 的 atr 列（若 screener 补充）→ 无 ATR 时 buffer=0
    # （退化为精确谷底止损，亦符合蔡森原典"停损=C波低点"）。
    atr_val = float(row["atr"]) if "atr" in row and pd.notna(row.get("atr")) else 0.0
    stop_loss = bottom_price - cfg.stop_loss_atr_buffer * atr_val

    # —— 5. 盈亏比校验（Bug4：回踩入场 + 第 n 波目标的真实盈亏比）——
    # 旧公式 (take_profit - breakout)/(breakout - stop) 用【突破价】作入场价，但本策略是
    # 【回踩入场】（在 entry_lower..entry_upper 区间挂单），实际成交价低于突破价；且目标
    # 应用第 n 波满足（cfg.neckline_height_multiple，默认 2）而非第一波。旧公式数学上
    # 分子永远 < H、分母永远 > H → rr 必 < 1.0，被 min_rr_ratio 全拦（发不出任何计划）。
    # 新公式：expected_entry = 回踩区间均价；target = 第 n 波满足；risk = expected_entry - stop。
    expected_entry = (entry_upper + entry_lower) / 2.0
    risk_per_unit = expected_entry - stop_loss
    if risk_per_unit <= 0:
        # 回踩均价已跌破止损 = 形态破位（理论不应出现，脏数据防御），跳过
        return None
    rr = (take_profit_n - expected_entry) / risk_per_unit
    if rr < cfg.min_rr_ratio:
        return None

    # —— 6. 时间窗口（回踩触发 + 时间止损）——
    # valid_until      = formed_at + pullback_window_bars 交易日
    # max_holding_until = formed_at + max_holding_bars 交易日
    # trading_calendar 提供时按日历索引推进（精确）；缺省时 bdate_range 跳周末兜底。
    valid_until = _advance_trading_days(formed_at, cfg.pullback_window_bars, trading_calendar)
    max_holding_until = _advance_trading_days(formed_at, cfg.max_holding_bars, trading_calendar)

    # —— 7. 仓位分配（固定风险 + 5% 市值硬钳 + A股整手）——
    shares = risk.position_size(aum, entry_upper, stop_loss, coef)

    # —— 8. 组装 TradePlan（frozen 值对象）——
    # plan_id 用 uuid4 保证跨服务唯一（执行器/日志/数据库主键关联）。
    # metadata 记录 bottom_price 反推来源 + atr 来源 + 第 n 波满足价，便于审计追溯。
    return TradePlan(
        plan_id=uuid.uuid4().hex,
        symbol=symbol,
        pattern_type=pattern_type,
        formed_at=formed_at,
        breakout_price=breakout_price,
        neckline_price=neckline_price,
        bottom_price=bottom_price,
        H=H,
        entry_upper=entry_upper,
        entry_lower=entry_lower,
        stop_loss=stop_loss,
        take_profit=take_profit,
        take_profit_2x=take_profit_2x,
        rr_ratio=rr,
        valid_until=valid_until,
        max_holding_until=max_holding_until,
        timeout_exit_threshold=cfg.timeout_exit_threshold,
        shares=shares,
        metadata={
            "depth": depth,
            "bottom_price_source": "pattern:min(p1,p3)|p4",
            "atr_source": "column" if atr_val > 0 else "none(buffer=0)",
            "neckline_height_multiple": n,
            "take_profit_n": take_profit_n,  # 第 n 波满足价（cfg.neckline_height_multiple 级）
        },
    )


def _advance_trading_days(
    start: pd.Timestamp,
    n: int,
    trading_calendar: Optional[pd.DatetimeIndex],
) -> pd.Timestamp:
    """从 start 推进 n 个交易日，返回目标交易日 Timestamp。

    优先级：
        1. trading_calendar 提供时：在日历中定位 start 位置，向后推 n 个交易日
           （日历不含 start 时，用 searchsorted 找 ≥ start 的最近日历点再推 n）；
        2. 缺省时：pd.bdate_range(start, periods=n+1) 跳周末兜底（periods=n+1 因含 start）。

    防御性：n ≤ 0 时直接返回 start（无推进，防负数/零的退化场景）。
    """
    if n <= 0:
        return start

    if trading_calendar is not None and len(trading_calendar) > 0:
        # 日历定位：找 ≥ start 的最近日历点（normalized 比较仅日期）
        cal = trading_calendar
        # start 可能带时分秒，用 normalize 对齐到日级再比较
        start_day = start.normalize()
        # searchsorted 返回 ≥ start_day 的插入位置（左侧）
        pos = cal.searchsorted(start_day, side="left")
        # 若 start 恰好是日历内日期（pos < len 且 cal[pos]==start_day），从此点 +n
        # 否则从 pos 点（>start 的下一日历日）+n（保守向前看，避免回溯到 start 之前）
        target_idx = pos + n
        if target_idx < len(cal):
            return pd.Timestamp(cal[target_idx]).normalize()
        # 超出日历范围：fallback bdate_range（保证不抛异常）
        return pd.Timestamp(pd.bdate_range(start, periods=n + 1)[-1]).normalize()

    # 无交易日历：bdate_range 跳周末兜底（periods=n+1 因序列含 start 占 1 位）
    return pd.Timestamp(pd.bdate_range(start, periods=n + 1)[-1]).normalize()
