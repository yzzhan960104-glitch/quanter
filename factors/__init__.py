# -*- coding: utf-8 -*-
"""因子层：纯计算因子（ATR 等），从 core/ 解散迁入（归属：模型层·因子）。

factors/ 此前是空壳目录（连 __init__.py 都没有），Step1 首次落地——
原本规划的抽象层开始成型（design §5.3「债务清零」信号）。
"""
from .atr import atr

__all__ = ["atr"]
