# -*- coding: utf-8 -*-
"""engines/ 策略本体（纯逻辑·无 IO）—— 单向依赖红线：optimize/infra 依赖本包，本包绝不反向 import 它们。

Task 1.3（Layer2 解耦·caisen 形态退役）：
    plan / risk / config / patterns 整子包已全删（caisen 形态 W底/头肩/三角形退役）。
    exit_logic 已由 Task 1.2 迁至 execution/exit_logic.py（杀手不变量单源）。
    本包现为空壳（caisen 包整体瘦身见 caisen/__init__.py），保留 __init__ 以维持
    caisen.engines 作为合法子包路径（部分垫片/历史 import 可能仍引用 caisen.engines）。
"""
# Task 1.3：plan/risk/config/patterns 实体全删，无 re-export。
