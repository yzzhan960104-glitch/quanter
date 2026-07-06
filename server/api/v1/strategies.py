# -*- coding: utf-8 -*-
"""策略查询路由

职责：
1. GET /api/v1/strategies —— 列出已注册策略（供前端下拉框）
2. GET /api/v1/strategies/{name}/schema —— 返回策略参数 JSON Schema（供前端动态渲染表单）

设计原则：
- 路由层只读取 app.state.strategy_loader（启动时扫描注册），不重复扫描
  Why：扫描走 importlib 较重（每请求都 import 全部策略模块），且策略注册表
  在进程生命周期内不变，放 lifespan 启动期一次性完成最合理。
- schema 来自 params_model.model_json_schema()，单一真相源：策略改字段，
  前端表单自动跟随，无需手写表单配置。
- KeyError → 404：loader 对未注册策略抛 KeyError，路由层转成 HTTP 404，
  避免 500 误导前端以为是服务端故障。
"""
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from server.schemas.strategy import StrategyTopology, ExecutionPlan
from server.services import strategy_service

router = APIRouter(prefix="/strategies", tags=["策略"])


def _get_loader(request: Request):
    """从 app.state 取启动时扫描的 StrategyLoader 单例

    Why 防御性检查：若 lifespan 未触发（如误用裸 TestClient 或测试未走 with
    上下文），app.state.strategy_loader 会缺失。此时返回 500 明确暴露初始化
    问题，而非让后续 .list() 抛 AttributeError 产生隐晦错误。
    """
    loader = getattr(request.app.state, "strategy_loader", None)
    if loader is None:
        raise HTTPException(status_code=500, detail="策略加载器未初始化")
    return loader


@router.get("", response_model=List[StrategyTopology], summary="列出已注册策略")
async def list_strategies(request: Request) -> List[StrategyTopology]:
    """返回启动时扫描注册的全部策略（供前端下拉框 + 策略拓扑视图）

    每条结构：{name, label, universe, composition, rhythm, capital_allocation}
    - name: 策略唯一标识（前端下拉框 value，回测请求体里 strategy 字段用它）
    - label: 中文展示名（前端下拉框 label）
    - universe: 标的池（类层面声明，仅参考；实际回测以请求体 universe 为准）
    - composition: {factors, datasets}（层级三拓扑白盒 + 因子反查引用）
    - rhythm: 交易节奏（超短频/日频/周频）
    - capital_allocation: 资金分配逻辑（人类可读描述）
    """
    return _get_loader(request).list()


@router.get("/{name}/schema", summary="获取策略参数 JSON Schema")
async def get_strategy_schema(name: str, request: Request) -> Dict[str, Any]:
    """返回策略 params_model 的 JSON Schema（含 ui 渲染提示）

    前端动态渲染器依据本 Schema 生成表单：
    - type/properties/required：标准 JSON Schema 字段
    - 每字段内的 ui.control（slider/number/select 等）：自定义渲染提示
      （经 Pydantic Field json_schema_extra 合并进来）
    """
    loader = _get_loader(request)
    try:
        return loader.get_schema(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{name}/plan", response_model=ExecutionPlan, summary="策略执行计划 DAG")
async def get_strategy_plan(name: str, request: Request) -> ExecutionPlan:
    """派生策略执行计划：数据拉取 → 因子计算 → 信号融合 → 风控/下单（依赖树 DAG）。

    由 composition + rhythm + capital_allocation + universe 派生标准四阶段；
    前端 ECharts graph 按 nodes + depends_on 渲染生命周期。
    """
    try:
        return ExecutionPlan(**strategy_service.build_execution_plan(_get_loader(request), name))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
