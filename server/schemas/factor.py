# -*- coding: utf-8 -*-
"""层级二·因子注册表的 Pydantic 契约。

- FactorSummary：/factors/registry 返回项（因子矩阵卡片数据源）。
- FactorDetail：单因子 drill-down（元数据 + 数据集 + 引用策略 + IC 衰减）。
- ICDecayPoint / ICHeatmap：IC/IR 衰减曲线与月度×horizon 热力图。
"""
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel

FactorStatus = Literal["training", "live", "deprecated"]
FactorInputKind = Literal["returns_panel", "ohlcv_panel", "lake_series", "cross_section", "set"]


class FactorSummary(BaseModel):
    """因子摘要（GET /factors/registry 返回项）。"""
    name: str
    label: str
    category: str
    author: str
    status: FactorStatus
    input_kind: FactorInputKind
    dataset: str
    description: str
    grid_computable: bool
    default_params: Dict[str, Any]


class StrategyRef(BaseModel):
    """引用了某因子的策略（drill-down 展示）。"""
    name: str
    label: str


class ICDecayPoint(BaseModel):
    """单 horizon 的 IC 衰减点（IC 随持有期延长而衰减的曲线节点）。"""
    horizon: int            # 远期收益持有期（交易日）
    ic_mean: float          # 该 horizon 下 IC 均值（预测力）
    ic_ir: float            # IC 信息比（IC均值/IC标准差，稳定性）
    t_stat: float           # t 统计量（显著性检验）


class ICHeatmap(BaseModel):
    """月度 × horizon 的 IC 热力图（ECharts heatmap 直消费）。"""
    months: List[str]               # Y 轴：年-月
    horizons: List[int]             # X 轴：持有期
    data: List[List[Any]]           # [month_idx, horizon_idx, ic] 三元组列表


class ICDecayResult(BaseModel):
    """GET /factors/{name}/ic_decay 响应。"""
    ok: bool
    name: str
    label: Optional[str] = None
    reason: Optional[str] = None        # ok=False 时的原因（非面板因子/无数据等）
    n_symbols: Optional[int] = None
    decay: List[ICDecayPoint] = []
    heatmap: Optional[ICHeatmap] = None


class FactorDetail(BaseModel):
    """GET /factors/{name} 响应（drill-down 头部信息）。"""
    summary: FactorSummary
    datasets: List[str]                 # 关联数据集 lake key 列表
    referenced_by: List[StrategyRef]    # 被哪些策略引用（Layer 3 composition 接入后填充）
