"""蔡森多空转折形态学流水线（纯多头）。

Step3 分包后：策略本体在 engines/、参数优化在 optimize/、执行/存储/回放在 infra/、
AI 决策预留 advisor/。本 __init__ re-export 旧顶层路径，保证 ``from caisen import ...``
等历史用法零改动（strangler 铁律①）。

注：``from caisen.config import X`` / ``from caisen.plan import X`` 这种【绝对模块路径】
不能只靠 __init__ 属性赋值兜底——Python 必须找到物理模块 caisen/config.py 才能解析。
故 caisen/ 顶层保留 1 行转发垫片模块（config.py / plan.py / risk.py），转发到 engines/ 新位置。
本 __init__ 的 re-export 主要服务 ``from caisen import StrategyConfig`` 这种包级取属性用法。
"""
# 旧顶层路径 re-export（物理文件已迁入 engines/，此处转发）
from caisen.engines.plan import *  # noqa: F401,F403
from caisen.engines.risk import *  # noqa: F401,F403
from caisen.engines.config import StrategyConfig  # noqa: F401
