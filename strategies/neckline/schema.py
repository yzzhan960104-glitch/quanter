# -*- coding: utf-8 -*-
"""颈线法策略参数模型（NecklineConfig · 18 维 = 识别层 11 + 执行层 7）。

阶段B：颈线法从 scripts/ 升格为 strategies/ 包的正式策略，参数对齐 scripts 的
DEFAULTS（识别层）+ EXEC_DEFAULTS（执行层）。供 ParamLab 前端反射 + training_analyzer
parse_review 字段护栏（替代 caisen 的 StrategyConfig 33 字段）。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class NecklineConfig(BaseModel):
    """颈线法 21 维参数（识别层 11 + 执行层 7 + trailing 3）。

    识别层（颈线形态判定）：window/min_touches/min_suppression/local_extrema_window/
    min_bottoms/breakout_vol_mult/min_rr/max_h_atr/stop_atr_mult/tp_h_mult/decay_tau。
    执行层（挂单/止盈/仓位/撤单）：max_holding/max_wait/cooldown/buy_limit_atr_mult/
    tp1_h_mult/tp1_portion/cancel_thresh_mult。
    trailing 层（时间驱动移动止损 · 海龟风格）：trailing_grace/trailing_step/trailing_floor。
    """

    # —— 识别层（11 维，对齐 neckline_method_v0.DEFAULTS）——
    window: int = Field(60, ge=20, le=120, description="颈线识别窗口（近 N 日）")
    min_touches: int = Field(2, ge=2, description="颈线由 ≥N 个顶部聚集连成")
    min_suppression: float = Field(0.6, ge=0.0, le=1.0, description="压制时长下限（close<颈线比例）")
    local_extrema_window: int = Field(3, ge=1, description="局部极值左右窗")
    min_bottoms: int = Field(2, ge=2, description="至少双底（含窗口最低点）")
    breakout_vol_mult: float = Field(1.5, ge=1.0, description="突破放量倍数（vs 近5日均量）")
    min_rr: float = Field(1.5, ge=0.5, description="盈亏比下限（结构恒 2.0，sanity 守卫）")
    max_h_atr: float = Field(4.0, ge=1.0, description="形态深度上限 H/ATR（防暴跌反弹）")
    stop_atr_mult: float = Field(1.0, ge=0.0, description="止损 ATR 倍数（颈线−N×ATR）")
    tp_h_mult: float = Field(2.0, ge=1.0, description="止盈2 H 倍数（颈线+N×H）")
    decay_tau: Optional[float] = Field(None, description="颈线聚集时间衰减（None=等权）")

    # —— 执行层（7 维，对齐 neckline_backtest.EXEC_DEFAULTS）——
    max_holding: int = Field(15, ge=1, description="成交后超时持仓日")
    max_wait: int = Field(5, ge=1, description="挂单等待回踩成交有效期")
    cooldown: int = Field(5, ge=0, description="信号去重冷却（相邻信号合并）")
    buy_limit_atr_mult: float = Field(1.0, description="挂单价 = 颈线 + N×ATR")
    tp1_h_mult: float = Field(1.0, description="止盈1 = 颈线 + N×H（第一波减仓）")
    tp1_portion: float = Field(0.5, ge=0.0, le=1.0, description="止盈1 减仓比例（lot1 占比）")
    cancel_thresh_mult: Optional[float] = Field(1.0, description="撤单阈值 = 颈线 + N×H（None=不撤单放飞）")

    # —— trailing 层（3 维，U5 · 2026-07-29 Task 8 收口；对齐 backtest.EXEC_DEFAULTS）——
    # 物理意图（海龟风格，用户 2026-07-20）：前 grace 天宽限（止损=颈线−stop_mult×ATR 固定不动，
    # 给趋势确认空间）；grace 天后每日收紧 step×ATR（趋势不确认逐步退出）；到 floor×ATR 卡住。
    # 默认值对齐 EXEC_DEFAULTS（grace=0/step=0/floor=0.5），即【默认退化为固定止损】——
    # 旧行为零回归，仅当显式传参才启用 trailing。compute_stop_price / decide_exit 单源消费。
    trailing_grace: int = Field(0, ge=0, description="trailing 宽限天数 b（前 b 天不收紧，给趋势确认空间；0=无宽限即日收紧）")
    trailing_step: float = Field(0.0, ge=0.0, description="trailing 收紧速度 a（ATR/日；0=固定止损退化为旧默认）")
    trailing_floor: float = Field(0.5, ge=0.0, description="trailing 最低 ATR 倍数（收紧上限；0=收到颈线，0.5=颈线−0.5ATR）")
