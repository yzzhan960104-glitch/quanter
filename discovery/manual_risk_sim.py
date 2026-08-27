# -*- coding: utf-8 -*-
"""人工风控模拟线（R3 · 2026-08-23 用户裁决的评估假设层）。

物理定位：
    用户裁决：「震荡市与趋势涨势期间尽量最大盈利，宏观层面变化导致的深度回撤
    由人工风控保证」——策略的目标函数是可交易期收益，宏观回撤敞口归人工职权
    （ADR-16 延伸：RISK_BLOCK.flag 拦增量语义）。本模块把这条哲学翻译成**回测
    评估假设**：池子等权滚动回撤跌破触发阈值的时段，模拟「人工 touch 了
    RISK_BLOCK.flag」——新入场信号被跳过（拦增量），存量持仓照常管理（保守
    下界：人工若同时手动清仓只会更好）。

🔴 ADR-16 红线（工程隔离，tests/test_layer_contract 家族钉死）：
    本模块是**回测/搜索评估假设**，不是实盘自动风控——`trading/` 永不 import
    本模块。实盘的宏观择时判断权 100% 归人工；任何「把这个日历接到实盘执行
    路径」的改动都是对本模块存在意义的违背。

阈值依据（docs/superpowers/plans/2026-08-23-r3-tradable-window-max.md §1.1 +
diag/r3_threshold_calibration.py 正式校准）：
    主口径 20 日窗/−15% 触发/−10% 解除（滞回）——2021-2024 选段校准 + 2026 年
    7 月验证（恰好覆盖 7/17-08/03 主跌段尾部，且 3 月 −11% 正常震荡不误拦）。

接口（两个纯函数，无状态可单测）：
    build_block_calendar(universe, rule) → frozenset[date]   # 预计算一次，O(1) 查表
    is_blocked(day, calendar) → bool                          # 评估热路径消费
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

# 默认主口径（R3 方案定稿；变更须走 ADR-15 同款修订留痕——阈值是评估语义不是普通参数）
DEFAULT_RULE = dict(window=20, trigger=-0.15, release=-0.10)


@dataclass(frozen=True)
class ManualRiskRule:
    """模拟线规则三件：滚动窗宽 / 触发阈值 / 解除阈值（滞回防抖）。"""
    window: int = 20
    trigger: float = -0.15
    release: float = -0.10

    def __post_init__(self):
        if self.window < 2:
            raise ValueError(f"window 须 ≥2（滚动回看才有意义）：{self.window}")
        if not (self.trigger < self.release < 0):
            # 滞回几何：trigger < release < 0（如 −0.15 < −0.10 < 0）——触发更深、
            # 解除更浅，两阈值之间抖动不反复开关（防抖带）。
            raise ValueError(f"须满足 trigger < release < 0（滞回带）："
                             f"{self.trigger} !< {self.release}")


def _pool_equity(universe: dict) -> pd.Series:
    """池子等权累计净值（冻结 universe → 日收益等权均值 → 累积）。

    Why 等权而非市值加权：universe 是创板科创流动性前 N 池（freeze 已筛），等权
    是「策略持仓域的中性市场温度计」——与 beta 捕获率视图的池子口径同源。
    当日停牌股（NaN 收益）自动跳过（mean skipna）。
    """
    closes = {s: df["close"] for s, df in universe.items()
              if isinstance(df, pd.DataFrame) and "close" in getattr(df, "columns", [])}
    if not closes:
        raise ValueError("universe 无 close 列——形态异变（freeze 契约破坏）")
    wide = pd.DataFrame(closes)
    daily = (wide / wide.shift(1) - 1.0).mean(axis=1)   # 等权日收益（停牌 skipna）
    daily = daily.dropna()
    return (1.0 + daily).cumprod()


def build_block_calendar(universe: dict, rule: ManualRiskRule | None = None,
                         start=None, end=None) -> frozenset:
    """池子等权滚动回撤 → 滞回状态机 → 被拦交易日集合（frozenset[date]）。

    语义（对齐 RISK_BLOCK.flag 拦增量）：
        滚动回撤 dd = cum / rolling(window).max() − 1；
        dd ≤ trigger → 进入拦截；拦截中 dd ≥ release → 解除；
        首窗（前 window−1 日 dd=NaN）与数据外恒不拦。

    start/end（可选）：日历裁剪窗（评估段外的不拦，省内存）；默认全期。
    幂等纯函数：同 universe + 同 rule 逐字节一致（无随机无时钟）。
    """
    r = rule or ManualRiskRule(**DEFAULT_RULE)
    cum = _pool_equity(universe)
    if start is not None:
        cum = cum[cum.index >= pd.Timestamp(start)]
    if end is not None:
        cum = cum[cum.index <= pd.Timestamp(end)]
    if len(cum) < r.window:
        return frozenset()
    dd = cum / cum.rolling(r.window).max() - 1.0

    blocked_days, blocked = [], False
    for day, v in dd.items():
        if pd.isna(v):
            continue                                  # 首窗：信息不足，不拦
        if not blocked and v <= r.trigger:
            blocked = True
        elif blocked and v >= r.release:
            blocked = False
        if blocked:
            blocked_days.append(day.date())
    return frozenset(blocked_days)


def is_blocked(day, calendar: frozenset) -> bool:
    """评估热路径查表（O(1)）。day 容忍 date/datetime/Timestamp（统一折 date）。"""
    d = day.date() if hasattr(day, "date") and callable(getattr(day, "date", None)) else day
    return d in calendar
