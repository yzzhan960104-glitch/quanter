# -*- coding: utf-8 -*-
"""兼容垫片（Step1 迁移）：atr 已迁至 factors.atr。

strangler 铁律①：保留旧模块路径转发，`from core.indicator import atr` 零改动。
新代码请用 `from factors.atr import atr`。
"""
from factors.atr import atr  # noqa: F401

__all__ = ["atr"]
