# -*- coding: utf-8 -*-
"""层级三·策略拓扑与执行计划的 Pydantic 契约。

- StrategyTopology：/strategies 返回项（含 composition/rhythm/capital_allocation）。
- ExecutionPlanNode / ExecutionPlan：/strategies/{name}/plan 返回的依赖树 DAG。
"""
from typing import Any, Dict, List

from pydantic import BaseModel


class StrategyTopology(BaseModel):
    """策略拓扑（GET /strategies 返回项，扩展自原 name/label/universe）。"""
    name: str
    label: str
    universe: List[str]
    composition: Dict[str, Any]            # {"factors": [...], "datasets": [...]}
    rhythm: str                            # 超短频/日频/周频
    capital_allocation: str


class ExecutionPlanNode(BaseModel):
    """执行计划 DAG 节点（数据→因子→信号→下单 生命周期）。"""
    id: str                                # 节点唯一 id（如 data / factor:MACD / signal / order）
    label: str                             # 中文展示名
    stage: str                             # 阶段：data / factor / signal / order
    detail: str = ""                       # 节点详情（多行，前端换行展示）
    depends_on: List[str] = []             # 依赖节点 id 列表（DAG 边）


class ExecutionPlan(BaseModel):
    """GET /strategies/{name}/plan 响应（依赖树渲染「数据→因子→信号→下单」生命周期）。"""
    strategy: str
    label: str
    rhythm: str
    nodes: List[ExecutionPlanNode]
