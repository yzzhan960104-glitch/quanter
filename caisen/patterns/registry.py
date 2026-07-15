# -*- coding: utf-8 -*-
"""【转发垫片】registry.py → caisen/engines/patterns/registry.py（Step3.2 批 B）。

sys.modules 别名：使 ``caisen.patterns.registry`` 与 ``caisen.engines.patterns.registry``
成为同一模块对象，PATTERNS 等全局在任何路径下 patch 都一致生效。
"""
from caisen.engines.patterns import registry as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
