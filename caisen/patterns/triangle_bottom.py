# -*- coding: utf-8 -*-
"""【转发垫片】triangle_bottom.py → caisen/engines/patterns/triangle_bottom.py（Step3.2 批 B）。

sys.modules 别名：旧路径与 engines 新路径为同一模块对象，detect 等函数身份一致。
"""
from caisen.engines.patterns import triangle_bottom as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
