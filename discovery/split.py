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


# ============================================================================
# A2（2026-08-14 · DG-G4）：扩展切分——inner 各年 min calmar 的考场
# ============================================================================
def extended_split(embargo_days=5):
    """A2 扩展切分：inner 2021-2024（四个自然年，含 2022 熊市考场）/ outer 2025-2026。

    Why 扩窗：inner 只有 2025 一个自然年时「各年 min」退化为单年，无判别力——
    2026-08-14 实弹验证冠军 25c602 的 wf 四折折外 0.00/-0.62/1.58/3.93，整段
    inner calmar 44.87 全靠选择段（2025）撑起，「2025 特化」坐实。扩到 2021 起
    让 2022 单边熊 + 2023 震荡 + 2024 结构牛都成为考场，min 才有牙齿。

    Why 不改 holdout_split 默认：45 个 caller（compute_unit/publish/proposals/cli）
    依赖 2025/2026 口径作对照锚（含 oos/wf4 交叉验证）；扩展切分仅搜索侧
    （cmd_run/cmd_daemon）启用，_split_tag 自然产 'holdout_2021_2025' 区分新旧。

    Why 2021 起非 2020：湖深 P0-2 实证 2016 起全覆盖；2020 单边牛对 min-calmar
    无增量判别力（牛年人人及格），多一年数据量 +33% 纯耗预算；2021 抱团瓦解
    震荡年恰是好的第一考场。
    """
    return HoldoutSplit(
        inner=Segment("inner_2021_24", date(2021, 1, 1), date(2024, 12, 31)),
        outer=Segment("outer_2025_26", date(2025, 1, 1), date(2026, 12, 31)),
        embargo_days=embargo_days,
    )


# ============================================================================
# P5（2026-08-13 · spec §6.1）：多折 walk-forward（数据可行性由 P0-2 实证：
# 创板科创 2020=1065 → 2026=2017 只/年，各折 >> 200 阈值；湖深 2016-07 起）
# ============================================================================
@dataclass
class WalkForwardSplit:
    """P5 多折 walk-forward：4 训练折（各带次年为 OOS）+ 终局 2026 去偏。

    折结构（经典锚定 walk-forward）：
      wf1: train 2020-21 → oos 2022
      wf2: train 2022-23 → oos 2024
      wf3: train 2024    → oos 2025
      wf4: train 2025    → oos 2026（= 现有二段 holdout 口径，交叉验证锚）
    科创约束（P0-2）：688/689 2019-07 开板，2020-21 折 universe = 创板全量 +
    科创（2020 起 ~211 只，占 ~20%）——evaluate_wf 的每折 universe 独立重建
    （折末 30 日流动性），不复用 2025 标的池（幸存者偏差防线）。
    """
    folds: list   # [(name, train: Segment, oos: Segment), ...]
    final_oos: Segment
    embargo_days: int


def walk_forward_split(embargo_days=5):
    """P5 四折 walk-forward 切分（embargo 语义与 holdout_split 同源）。"""
    return WalkForwardSplit(
        folds=[
            ("wf1_2020_21",
             Segment("t_2020_21", date(2020, 1, 1), date(2021, 12, 31)),
             Segment("o_2022", date(2022, 1, 1), date(2022, 12, 31))),
            ("wf2_2022_23",
             Segment("t_2022_23", date(2022, 1, 1), date(2023, 12, 31)),
             Segment("o_2024", date(2024, 1, 1), date(2024, 12, 31))),
            ("wf3_2024",
             Segment("t_2024", date(2024, 1, 1), date(2024, 12, 31)),
             Segment("o_2025", date(2025, 1, 1), date(2025, 12, 31))),
            ("wf4_2025",
             Segment("t_2025", date(2025, 1, 1), date(2025, 12, 31)),
             Segment("o_2026", date(2026, 1, 1), date(2026, 12, 31))),
        ],
        final_oos=Segment("oos_2026", date(2026, 1, 1), date(2026, 12, 31)),
        embargo_days=embargo_days,
    )
