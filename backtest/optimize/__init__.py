# -*- coding: utf-8 -*-
"""backtest/optimize/ 参数优化（可异步·可重跑）——颈线法参数训练 generic 基础设施。

Layer2 阶段4（spec §3.6）：training_analyzer / training_loops_db / training_loop /
training_dingtalk 四实体由 caisen/optimize/ 整体迁入本包。回测求变与交易求稳分离后，
参数训练与回测 driver 同处 backtest/ 包（training_loop → tasks_db → worker 调
backtest.replay，闭环自洽）。caisen/ 包随之解散（无真身残留）。

依赖方向（不变量）：本子包仅依赖 backtest（tasks_db 时间戳工具 + replay driver）
+ server.services.review_service（AI 分析横切共享 _call_glm，Spec3 历史债白名单）
+ core.notifier（钉钉推送）+ stdlib。不触 trading.engine/execution/broker。
"""
from .training_analyzer import *  # noqa: F401,F403
from .training_loops_db import *  # noqa: F401,F403
from .training_loop import *  # noqa: F401,F403
from .training_dingtalk import *  # noqa: F401,F403
