# -*- coding: utf-8 -*-
"""Celery 因子沙盒配置（执行编排）—— 从 config.py 拆出（归属：执行编排）。

持有 Celery broker/队列/CPU 闸门配置，broker_url/queue 通过 _os.getenv 从环境变量注入。
"""
import os as _os

# Celery 因子沙盒（Epic 3）
# cpu_gate_percent: CPU 占用闸门，超过该阈值则降级/排队，
# 防止因子全量重算压垮实时交易宿主机。
CELERY_CONFIG = {
    "broker_url": _os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    "queue": _os.getenv("CELERY_EXPLORER_QUEUE", "explorer"),
    "cpu_gate_percent": 80.0,
}
