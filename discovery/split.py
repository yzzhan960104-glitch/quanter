# -*- coding: utf-8 -*-
"""L1 嵌套验证切分（spec §3.3，Plan 1 简化版：2025/2026 holdout）。

Plan 1 不做 4 折 walk-forward（需 2020-2024 数据 + universe 时点决策，推后续 plan），
退化为二段 holdout：inner=2025（样本内诊断）/ outer=2026（OOS 去偏锚）。
embargo 吸收 2025→2026 边界的 trailing 持仓跨越（颈线法 trailing grace/max_holding
可达数日~20 日，2025 末信号持仓可能跨到 2026 初；embargo 让 outer 评估跳过这段，
防 inner 持仓污染 outer）。
"""
from dataclasses import dataclass
from datetime import date


@dataclass
class Segment:
    """一段日期区间（inner test / outer holdout）。"""
    name: str
    start: date
    end: date

    def covers(self, d):
        """d 是否落在本段（含端点）。d 可为 date 或 pandas Timestamp。"""
        dd = d.date() if hasattr(d, "date") and callable(getattr(d, "date", None)) else d
        return self.start <= dd <= self.end


@dataclass
class HoldoutSplit:
    """Plan 1 二段切分。"""
    inner: Segment          # 2025（样本内诊断）
    outer: Segment          # 2026（OOS 去偏锚，不反馈搜索）
    embargo_days: int       # inner→outer 边界 embargo（吸收持仓跨越）


def holdout_split(embargo_days=5):
    """Plan 1 二段切分：inner 2025 / outer 2026。

    embargo_days 默认 5（颈线法 2025 末信号 max_holding≤20，但跨年的多为短线回踩，
    5 日吸收绝大多数；后续可按 trailing 配置调）。objective 在分段时会用 outer 段
    起点向后让 embargo_days 天，跳过边界持仓。
    """
    return HoldoutSplit(
        inner=Segment("inner_2025", date(2025, 1, 1), date(2025, 12, 31)),
        outer=Segment("outer_2026", date(2026, 1, 1), date(2026, 12, 31)),
        embargo_days=embargo_days,
    )
