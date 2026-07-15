# -*- coding: utf-8 -*-
"""【转发垫片】head_shoulder.py → caisen/engines/patterns/head_shoulder.py（Step3.2 批 B）。

sys.modules 别名：旧路径与 engines 新路径为同一模块对象，detect 等函数身份一致。
"""
from caisen.engines.patterns import head_shoulder as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
