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
# Step3.4：infra storage/execution/replay_*/viz_* 物理迁移后，预先触发【真实模块】进入
# sys.modules，使顶层 caisen.<infra> 垫片的 sys.modules 别名在「垫片先于 infra 包被导入」
# 的顺序下仍能绑定到同一真实模块对象（与 engines/optimize 同模式）。
# Task3.3 沉淀：凡被 ``from caisen import X`` 引用的迁移模块须同步加预加载行。
#
# Step4c 批 A：storage + execution（engine）已从 caisen/infra/ 物理迁入 execution/ 顶层包。
# 真身改在 execution.storage / execution.engine；caisen.infra.storage / caisen.infra.execution
# 降为转发垫片（指向 execution.*）。预加载源随之改指 execution.*，使 caisen.storage /
# caisen.execution 顶层垫片 → caisen.infra.* 垫片 → execution.* 真身 三层同源。
from execution.storage import *  # noqa: F401,F403
from execution.engine import *  # noqa: F401,F403
# 批 B：5 个 replay_* 模块。facade 依赖 backtest_replay/replay_runs/replay_tasks_db 三个名。
# Step4c 批 B：5 个 replay 模块已从 caisen/infra/ 物理迁入 execution/。真身在 execution.*；
# caisen.infra.* 降为转发垫片。预加载源改指 execution.*（与批 A storage/execution 同模式）。
from execution.backtest_replay import *  # noqa: F401,F403
from execution.replay_runs import *  # noqa: F401,F403
from execution.replay_tasks_db import *  # noqa: F401,F403
from execution.replay_scheduler import *  # noqa: F401,F403
from execution.replay_worker import *  # noqa: F401,F403
# 批 C：2 个 viz_* 模块（横切可视化层）。
# Step4f 批 C：viz_static + viz_interactive 已从 caisen/infra/ 物理迁入横切 viz/ 顶层包。
# 真身改在 viz.viz_static / viz.viz_interactive；caisen.infra.viz_* 降为转发垫片
# （指向 viz.*）。预加载源随之改指 viz.*，使 caisen.viz_* 顶层垫片 → caisen.infra.viz_*
# 垫片 → viz.viz_* 真身 三层同源（与批 A/B storage/execution/replay 同模式）。
from viz.viz_static import *  # noqa: F401,F403
from viz.viz_interactive import *  # noqa: F401,F403
