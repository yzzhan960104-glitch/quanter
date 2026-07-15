# -*- coding: utf-8 -*-
"""engines/ 策略本体（纯逻辑·无 IO）—— 单向依赖红线：optimize/advisor/infra 依赖本包，本包绝不反向 import 它们。

Step3.2 物理迁移完成后：plan/risk/config/patterns 实体已全部落入本包。
- 新路径推荐：``from caisen.engines.plan import generate``
- 旧路径兼容：``from caisen.plan import generate`` 经 caisen/__init__.py + caisen 顶层垫片模块转发仍可用（strangler 铁律①）。
"""
# 实体已全部迁入本包：re-export 改指相对新位置（.plan / .risk / .config / .patterns）
from .plan import *  # noqa: F401,F403
from .risk import *  # noqa: F401,F403
from .config import StrategyConfig  # noqa: F401
# patterns 整子包已迁入 .patterns（批 B 完成）
from .patterns.screener import PatternScreener  # noqa: F401
from .patterns import (  # noqa: F401
    w_bottom, head_shoulder, triangle_bottom, neckline, zigzag_causal, registry, screener,
)
