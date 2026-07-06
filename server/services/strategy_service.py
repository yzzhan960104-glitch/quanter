# -*- coding: utf-8 -*-
"""层级三·策略执行计划构建（从 composition/rhythm/capital_allocation 派生 DAG）。

设计（Karpathy 显式派生）：
- 不要求每个策略手写 execution_plan（避免重复声明）；由 composition + rhythm + capital_allocation
  + universe 统一派生标准四阶段 DAG：数据拉取 → 因子计算（每个因子一节点）→ 信号融合 → 风控/下单。
- 节点 id 规范：data / factor:{name} / signal / order，depends_on 显式连边，前端 ECharts graph 直消费。

拷问三连（已显式处置）：
- 成环：派生模板天然无环（线性 data→factor→signal→order，因子并行但不互依）。
- 空因子：composition.factors 为空时 signal 直接依赖 data（如纯规则策略不退化）。
- 长标的池：universe 详情只展示前 5 个 + 省略号，防节点 detail 撑爆图。
"""
from __future__ import annotations

from typing import Any, Dict, List

from strategies.loader import StrategyLoader


def build_execution_plan(loader: StrategyLoader, name: str) -> Dict[str, Any]:
    """派生策略执行计划 DAG。未注册策略抛 KeyError（路由层转 404）。"""
    cls = loader.get(name)
    composition = getattr(cls, "composition", {}) or {}
    factors: List[str] = list(composition.get("factors", []) or [])
    datasets: List[str] = list(composition.get("datasets", []) or [])
    universe: List[str] = list(getattr(cls, "universe", []) or [])
    rhythm: str = getattr(cls, "rhythm", "日频")
    cap: str = getattr(cls, "capital_allocation", "")

    nodes: List[Dict[str, Any]] = []

    # 阶段①：数据拉取（汇总数据集 + 标的池规模）
    ds_detail = "、".join(datasets) if datasets else "（未声明）"
    if universe:
        preview = ", ".join(universe[:5]) + ("…" if len(universe) > 5 else "")
        univ_detail = f"{len(universe)} 标的：{preview}"
    else:
        univ_detail = "标的池由请求注入"
    nodes.append({
        "id": "data", "label": "数据拉取", "stage": "data",
        "detail": f"数据集：{ds_detail}\n{univ_detail}", "depends_on": [],
    })

    # 阶段②：因子计算（每个因子一节点，并行依赖 data）
    factor_ids: List[str] = []
    for f in factors:
        nid = f"factor:{f}"
        factor_ids.append(nid)
        nodes.append({
            "id": nid, "label": f"因子：{f}", "stage": "factor",
            "detail": f, "depends_on": ["data"],
        })

    # 阶段③：信号融合（依赖全部因子；无因子则直依赖 data）
    sig_deps = factor_ids if factor_ids else ["data"]
    nodes.append({
        "id": "signal", "label": "信号融合", "stage": "signal",
        "detail": getattr(cls, "label", name), "depends_on": sig_deps,
    })

    # 阶段④：风控/下单（依赖 signal；展示节奏与资金分配逻辑）
    nodes.append({
        "id": "order", "label": "风控/下单", "stage": "order",
        "detail": f"节奏：{rhythm}\n{cap}", "depends_on": ["signal"],
    })

    return {
        "strategy": name,
        "label": getattr(cls, "label", name),
        "rhythm": rhythm,
        "nodes": nodes,
    }
