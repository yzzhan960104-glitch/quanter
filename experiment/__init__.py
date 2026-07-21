# -*- coding: utf-8 -*-
"""实验系统：实盘下单的策略版本配置中心（单一职责·零反向依赖）。

物理定位：管理「在线实验版本 + 资金权重」，scan 经 resolve_active() 获取当前生效
(strategy_name, params, weight) 列表。不生成 plan、不下单、不管持仓归因。
"""
from experiment.models import (  # noqa: F401
    ExperimentStatus, ExperimentVersion, AuditLog, ActiveExperiment,
    validate_transition, validate_weight_sum,
)
