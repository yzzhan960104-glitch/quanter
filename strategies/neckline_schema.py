# -*- coding: utf-8 -*-
"""【垫片·过渡】NecklineConfig re-export —— U1 契约归位后的兼容层。

物理定位：
    NecklineConfig（18 维参数 pydantic 模型）物理实体已迁至
    ``strategies/neckline/schema.py``（U1：颈线法识别/执行/契约统一收口到
    strategies/neckline/ 子包）。本文件保留在原路径 ``strategies/neckline_schema.py``
    仅作**向后兼容 re-export**，让任何漏改的调用点
    （``from strategies.neckline_schema import NecklineConfig``）继续可导入。

生命周期：
    - 本垫片在阶段 4（U4 契约废弃收尾）统一删除；
    - 新代码必须直接 ``from strategies.neckline.schema import NecklineConfig``，禁止依赖本垫片。

决策逻辑零改动：纯透传，不重新定义、不撒谎。
"""
from strategies.neckline.schema import NecklineConfig  # noqa: F401

__all__ = ["NecklineConfig"]
