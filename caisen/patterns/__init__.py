# -*- coding: utf-8 -*-
"""【转发垫片包】caisen/patterns/ —— 物理实体已迁至 caisen/engines/patterns/（Step3.2 批 B）。

存在原因：内部模块 + tests + scripts + server 大量使用 ``from caisen.patterns.X import Y``
或 ``from caisen.patterns import X as Y`` 这种【绝对模块路径】，Python 必须能 import 到真实的
``caisen.patterns`` 包对象及其子模块。仅靠 caisen/__init__.py 属性赋值无法满足该形式，
故保留本垫片包（__init__ + 每个子模块 1 行转发），零逻辑改动（strangler 铁律①）。

新代码请直接使用 ``from caisen.engines.patterns.X import ...``。
"""
# 形态识别子包：因果 ZigZag / 颈线 / W底 / 头肩底 / 收敛三角形底部 / 编排器。
# 触发各子模块对象绑定到 caisen.patterns 命名空间（让 `from caisen.patterns import neckline` 可用）
from caisen.engines.patterns import (  # noqa: F401
    w_bottom, head_shoulder, triangle_bottom, neckline, zigzag_causal, registry, screener,
)
