# -*- coding: utf-8 -*-
"""【垫片·过渡】Signal re-export —— U1 契约归位后的兼容层。

物理定位：
    Signal dataclass 物理实体已迁至 ``strategies/neckline/signal.py``（U1：颈线法
    识别/执行/契约统一收口到 strategies/neckline/ 子包）。本文件保留在原路径
    ``strategies/signal.py`` 仅作**向后兼容 re-export**，让任何漏改的调用点
    （``from strategies.signal import Signal``）继续可导入，避免机械搬迁引入回归。

生命周期：
    - 本垫片在阶段 4（U4 契约废弃收尾）统一删除；
    - 新代码必须直接 ``from strategies.neckline.signal import Signal``，禁止依赖本垫片。

决策逻辑零改动：纯透传，不重新定义、不撒谎。
"""
from strategies.neckline.signal import Signal, signal_to_dict  # noqa: F401

__all__ = ["Signal", "signal_to_dict"]
