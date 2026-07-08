# -*- coding: utf-8 -*-
"""Celery 实例（因子网格任务已随 factors 体系整体删除）。

当前职责：
    仅保留 Celery 单例与 task_default_queue 配置，供后续复用（如蔡森形态学
    流水线后续接入异步任务时可直接挂载 @celery_app.task）。

关键工程取舍（Why）：
- Celery app 为模块级单例，但实例化仅记录 broker_url，不在此刻连 Redis（lazy）；
  因此开发机/CI 无 Redis 时仍可正常 import 本模块——Redis 真正不可用只会在
  `.delay()` 时显式抛 redis.ConnectionError，由调用方按需降级，绝不阻断主流程。
- 原 run_factor_grid / run_factor_grid_impl 因强依赖 factors.analyzer /
  factors.exploratory_momentum，已在 Phase 1·Task 3 随 factors 体系整体删除。
  如后续需异步任务，应在新的领域模块内重新实现（不再回引因子框架）。
"""
from __future__ import annotations

from celery import Celery

from config import CELERY_CONFIG

# Why Celery(..., broker/backend)：单 Redis 同时承担消息中间件与结果后端，
# 极简拓扑、运维单点；实例化不建连接（lazy），保证无 Redis 也可 import。
celery_app = Celery("quanter",
                    broker=CELERY_CONFIG["broker_url"],
                    backend=CELERY_CONFIG["broker_url"])
celery_app.conf.task_default_queue = CELERY_CONFIG["queue"]
