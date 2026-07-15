# -*- coding: utf-8 -*-
"""engines/ 策略本体（纯逻辑·无 IO）—— 单向依赖红线：optimize/advisor/infra 依赖本包，本包绝不反向 import 它们。"""
# Step3a 阶段：文件仍在 caisen/ 顶层，此处 re-export 让新路径可用；3b 物理迁移后改指向。
from caisen.plan import *  # noqa: F401,F403
from caisen.risk import *  # noqa: F401,F403
from caisen.config import StrategyConfig  # noqa: F401
from caisen.patterns.screener import PatternScreener  # noqa: F401
# patterns 整子包新路径（3a 垫片，3b 物理移入 engines/patterns/）
# 实际 caisen/patterns/__init__.py 为空（仅 docstring），用 `from 包 import 子模块` 语法
# 触发 Python 自动 import 并绑定子模块对象（不需 __init__ 显式列出）。
from caisen.patterns import (  # noqa: F401
    w_bottom, head_shoulder, triangle_bottom, neckline, zigzag_causal, registry,
)
