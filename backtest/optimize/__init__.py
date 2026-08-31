# -*- coding: utf-8 -*-
"""backtest/optimize/ 参数优化（可异步·可重跑）——颈线法参数训练 generic 基础设施。

Layer2 阶段4（spec §3.6）：training_analyzer / training_loops_db / training_loop /
training_dingtalk 四实体由 caisen/optimize/ 整体迁入本包。回测求变与交易求稳分离后，
参数训练与回测 driver 同处 backtest/ 包（tasks_db → worker 调 backtest.replay，闭环
自洽）。caisen/ 包随之解散（无真身残留）。

历史（2026-08-31 写端点全量退役）：training_loops_db / training_loop /
training_dingtalk 三实体整删——HTTP start/stop/review 是 TrainingLoopOrchestrator
状态机唯一驱动入口，路由删除后状态机成死守护线程，连根退役。本包现存
training_analyzer（AI 分析纯函数，replay 链路仍在用）。

依赖方向（不变量）：本子包仅依赖 backtest（tasks_db 时间戳工具 + replay driver）
+ infra.llm（AI 分析横切，经 infra/llm 工厂调 LLM——Layer2 解耦 follow-up #3 已收口）
+ infra.notifier（钉钉推送）+ stdlib。不触 trading.engine/execution/broker。
"""
from .training_analyzer import *  # noqa: F401,F403
