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
# Step3.3：optimize training_* 物理迁移后，预先触发 caisen.optimize.training_* 的真实模块
# 进入 sys.modules，使顶层 caisen.training_* 垫片的 sys.modules 别名在「垫片先于 optimize 包
# 被导入」的顺序下仍能绑定到同一真实模块对象（否则 from caisen import training_analyzer 会
# 绑定到垫片壳子，monkeypatch caisen.training_analyzer._call_glm 失效）。与 engines 同模式。
from caisen.optimize.training_analyzer import *  # noqa: F401,F403
from caisen.optimize.training_loops_db import *  # noqa: F401,F403
from caisen.optimize.training_loop import *  # noqa: F401,F403
from caisen.optimize.training_dingtalk import *  # noqa: F401,F403
# Step3.4：infra storage/execution/replay_*/viz_* 物理迁移后，预先触发 caisen.infra.* 的
# 真实模块进入 sys.modules，使顶层 caisen.<infra> 垫片的 sys.modules 别名在「垫片先于
# infra 包被导入」的顺序下仍能绑定到同一真实模块对象（与 engines/optimize 同模式）。
# Task3.3 沉淀：凡被 ``from caisen import X`` 引用的迁移模块须同步加预加载行。
from caisen.infra.storage import *  # noqa: F401,F403
from caisen.infra.execution import *  # noqa: F401,F403
# 批 B：5 个 replay_* 模块。facade 依赖 backtest_replay/replay_runs/replay_tasks_db 三个名。
from caisen.infra.backtest_replay import *  # noqa: F401,F403
from caisen.infra.replay_runs import *  # noqa: F401,F403
from caisen.infra.replay_tasks_db import *  # noqa: F401,F403
from caisen.infra.replay_scheduler import *  # noqa: F401,F403
from caisen.infra.replay_worker import *  # noqa: F401,F403
# 批 C：2 个 viz_* 模块（横切可视化层，Step4 同迁出）。
from caisen.infra.viz_static import *  # noqa: F401,F403
from caisen.infra.viz_interactive import *  # noqa: F401,F403
