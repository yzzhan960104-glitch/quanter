# -*- coding: utf-8 -*-
"""【转发垫片】neckline.py → caisen/engines/patterns/neckline.py（Step3.2 批 B）。

sys.modules 别名：旧路径与 engines 新路径为同一模块对象。
"""
from caisen.engines.patterns import neckline as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
