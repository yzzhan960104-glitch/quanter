"""caisen 包入口（Layer2 解耦·Task 1.3 后瘦身壳）。

物理现状（Task 1.3 caisen 形态完整退役后）：
    - engines/：plan/risk/config/patterns 全删（caisen 形态 W底/头肩/三角形退役），
      exit_logic 已迁 execution/exit_logic.py（Task 1.2）。engines 现为空壳子包。
    - optimize/：保留（generic 参数训练，颈线法经 training_loop 使用，stage 4 迁出 caisen）。
    - infra/：保留垫片转发（replay_*/backtest_replay 真身在 execution/）。viz_* 垫片已随
      Task 1.4 caisen 形态可视化退役删除（viz_static/viz_interactive 真身 viz.viz_static/
      viz.viz_interactive 全删，pattern 可视化是 caisen 形态专属 dead code）。
    - advisor/：保留（如有）。
    - 顶层垫片：config.py/risk.py/plan.py/patterns/ 已删（真身全删）；storage.py/execution.py/
      backtest_replay.py/replay_*.py/training_*.py 保留垫片转发；viz_*.py 已删（Task 1.4）。

本 __init__ 仅做必要预加载（strangler 铁律①·保 ``from caisen import X`` 历史用法）：
    - optimize training_*：使顶层 caisen.training_* 垫片绑定同源真实模块。
    - execution replay_*：使顶层垫片绑定同源真实模块。
    不再 re-export engines.plan/risk/config/patterns（已删）与 execution.storage/engine
    （Task 1.3 #3 全删）、viz viz_*（Task 1.4 caisen 形态可视化退役全删）。
"""
# Task 1.3：engines plan/risk/config/patterns 全删，不再 re-export。
# Task 1.4：viz viz_static/viz_interactive 全删（caisen 形态可视化 dead code），不再 re-export。
# Step3.3：optimize training_* 物理迁移后，预先触发 caisen.optimize.training_* 的真实模块
# 进入 sys.modules，使顶层 caisen.training_* 垫片的 sys.modules 别名在「垫片先于 optimize 包
# 被导入」的顺序下仍能绑定到同一真实模块对象（否则 from caisen import training_analyzer 会
# 绑定到垫片壳子，monkeypatch caisen.training_analyzer._call_glm 失效）。
from caisen.optimize.training_analyzer import *  # noqa: F401,F403
from caisen.optimize.training_loops_db import *  # noqa: F401,F403
from caisen.optimize.training_loop import *  # noqa: F401,F403
from caisen.optimize.training_dingtalk import *  # noqa: F401,F403
# Step4c 批 B：5 个 replay_* 模块。真身在 execution/；caisen.infra.* 降为转发垫片。
# Task 1.3：execution.engine + execution.storage 已删（caisen 形态执行链退役），不再 re-export。
# 预加载源保留 execution.backtest_replay/replay_*（颈线法异步回测基础设施保留）。
from execution.backtest_replay import *  # noqa: F401,F403
from execution.replay_runs import *  # noqa: F401,F403
from execution.replay_tasks_db import *  # noqa: F401,F403
from execution.replay_scheduler import *  # noqa: F401,F403
from execution.replay_worker import *  # noqa: F401,F403
