# -*- coding: utf-8 -*-
"""蔡森形态学流水线 REST 路由（Phase 3 · Task 4）。

物理定位（CLAEDE.md 极简 + 显式原则）：
    本模块是蔡森形态学流水线的"对外 HTTP 契约层"——把 caisen_service 的六个编排函数
    （run_scan / list_plans / approve_plan / activate_plan / get_plan / run_replay）
    + chart/positions 占位端点封装为 REST 友好接口，前端/调度器只感知这一层。

    端点清单（prefix=/caisen，挂载时叠加 /api/v1 → /api/v1/caisen/...）：
        POST   /scan                     扫描（run_scan → list[CandidatePlan]）
        GET    /plans?status=            读盘 + status 过滤（list_plans）
        GET    /plans/{plan_id}          单计划查询（get_plan；KeyError→404）
        PATCH  /plans/{plan_id}          审核 approve/reject + edits（approve_plan）
        POST   /plans/{plan_id}/activate 激活 APPROVED→ARMED（activate_plan）
        GET    /plans/{plan_id}/chart    lightweight-charts 数据（占位，Task 6 完善）
        GET    /positions                形态学持仓（占位，后续富化 trading_service）
        POST   /replay                   历史回放（run_replay → ReplayReportResponse）

异常映射红线（CLAUDE.md 量化风控·边界审查 + Task 3 review I-1）：
    service 层透传三类异常，路由层负责转译为正确 HTTP 状态码：
        KeyError            → 404（plan_id 不存在，状态机不进 NULL）
        ValidationError     → 422（cfg_override 字段名/值非法，参数错误）
        ValueError          → 422（业务参数非法，如 review.action 非 approve/reject）

    service 层算法/IO 异常已被内部 try/except 降级返回空结果 + warning 日志，
    路由层收到合法空结果即返 200（非 500，杜绝算法噪声污染前端）。

    NaN 经 StrictJSONResponse 早抛（main.py default_response_class 已挂全局防线，
    本路由不重复清洗数值，与 service 层契约对齐）。

设计取舍（Why 这样写）：
    - 路由层零业务逻辑：仅异常转译 + 透传 service，所有数学内核在 Phase 2 完成；
    - 同步 service 函数直接调用（非 run_in_threadpool）：caisen_service 的 IO 是
      JSON 文件读写（毫秒级），无网络阻塞，FastAPI 同步路由自带线程池调度；
    - chart/positions 端点占位：Task 6 接 viz 层 + 后续形态学持仓富化，本任务先
      立契约（端点可达 + 200 + 基本结构），不阻断 Phase 3 前端集成。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from server.schemas.caisen import (
    CandidatePlan,
    PlanReview,
    ReplayReportResponse,
    ReplayRequest,
    ScanRequest,
)
from server.services import caisen_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/caisen", tags=["蔡森形态学"])


# ---------------------------------------------------------------------------
# 异常转译辅助：把 service 透传的异常映射到 HTTP 状态码
# ---------------------------------------------------------------------------
def _map_service_exception(exc: Exception) -> HTTPException:
    """service 透传异常 → HTTPException 状态码转译。

    映射规则（与 Task 3 service 层契约对齐）：
        KeyError        → 404（plan_id 不存在，状态机不进 NULL）
        ValidationError → 422（cfg_override 字段名/值非法）
        ValueError      → 422（业务参数非法，如 review.action 非法）

    Why 集中映射：路由层多个端点（PATCH/activate/scan/replay）都要做相同转译，
    抽出单点避免重复 + 一致性（未来扩展异常类型只改一处）。
    """
    if isinstance(exc, KeyError):
        # KeyError 消息含 plan_id（service 抛出时已带上下文），直接转 404
        return HTTPException(status_code=404, detail=f"计划不存在：{exc}")
    if isinstance(exc, ValidationError):
        # ValidationError 是 Pydantic 抛的，errors() 含字段名/约束/值详情
        return HTTPException(status_code=422, detail=f"参数校验失败：{exc.errors()}")
    if isinstance(exc, ValueError):
        # 业务参数非法（如 review.action 非 approve/reject）
        return HTTPException(status_code=422, detail=str(exc))
    # 兜底：未预期异常转 500（service 已降级，理论上不会到这里）
    return HTTPException(status_code=500, detail=f"未预期异常：{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 端点 1：POST /scan —— 触发扫描（screener→plan.generate→storage.save_plans）
# ---------------------------------------------------------------------------
@router.post("/scan", summary="触发当日形态学扫描")
async def scan(body: ScanRequest) -> List[CandidatePlan]:
    """扫描：合成/装配 price_data → screener → plan.generate → 落盘 → 返回 CandidatePlan。

    物理意图（蔡森流水线起点）：
        前端/调度器 POST {date, universe, cfg_override} 触发当日扫描，service 层
        串接 screener.screen → plan.generate → storage.save_plans → 读回 CandidatePlan。

    异常策略：
        - ValidationError/ValueError/KeyError 透传 → 本层转 422/404（参数/状态机错误）；
        - 算法/IO 异常 service 已降级返空列表 → 本层返 200 []（非 500）。
    """
    try:
        return caisen_service.run_scan(body)
    except (KeyError, ValidationError, ValueError) as exc:
        raise _map_service_exception(exc)


# ---------------------------------------------------------------------------
# 端点 2：GET /plans —— 读盘 + status 过滤
# ---------------------------------------------------------------------------
@router.get("/plans", summary="候选计划列表（可选 status 过滤）")
async def list_plans(status: Optional[str] = Query(None, description="状态过滤（APPROVED/ARMED/...）")) -> List[CandidatePlan]:
    """读盘：跨日期合并加载所有候选计划，可选按 status 精确过滤。

    物理意图：前端审核面板全量浏览候选计划，按状态机当前态分页/筛选展示。
    无 plans 文件时返 200 + []（不抛异常，离线/无扫描历史场景容错）。
    """
    return caisen_service.list_plans(status=status)


# ---------------------------------------------------------------------------
# 端点 3：GET /plans/{plan_id} —— 单计划查询
# ---------------------------------------------------------------------------
@router.get("/plans/{plan_id}", summary="单候选计划查询")
async def get_plan(plan_id: str) -> CandidatePlan:
    """按 plan_id 精确查询单个候选计划。

    异常策略：plan_id 不存在 → service 返 None → 本层转 404（状态机不进 NULL）。
    """
    result = caisen_service.get_plan(plan_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"计划不存在：plan_id={plan_id!r}")
    return result


# ---------------------------------------------------------------------------
# 端点 4：PATCH /plans/{plan_id} —— 审核（approve/reject + 微调）
# ---------------------------------------------------------------------------
@router.patch("/plans/{plan_id}", summary="人工审核计划（approve/reject + edits）")
async def review_plan(plan_id: str, body: PlanReview) -> CandidatePlan:
    """审核：根据 review.action 推进 status + 应用 edits 微调。

    物理意图（蔡森流水线审核节点）：
        approve → APPROVED（可继续 activate 进入 ARMED）；
        reject  → REJECTED（不再进入挂单流程）；
        edits   → 字段微调（如人工调整 stop_loss/take_profit）。

    异常策略：
        - KeyError → 404（plan_id 不存在）；
        - ValueError → 422（review.action 非法，非 approve/reject）。
    """
    try:
        return caisen_service.approve_plan(plan_id, body)
    except (KeyError, ValueError) as exc:
        raise _map_service_exception(exc)


# ---------------------------------------------------------------------------
# 端点 5：POST /plans/{plan_id}/activate —— APPROVED → ARMED
# ---------------------------------------------------------------------------
@router.post("/plans/{plan_id}/activate", summary="激活计划（APPROVED → ARMED）")
async def activate_plan(plan_id: str) -> CandidatePlan:
    """激活：置 status=ARMED（挂单待执行，同步进 active.json 供执行器读）。

    异常策略：plan_id 不存在 → KeyError → 本层转 404。
    """
    try:
        return caisen_service.activate_plan(plan_id)
    except KeyError as exc:
        raise _map_service_exception(exc)


# ---------------------------------------------------------------------------
# 端点 6：GET /plans/{plan_id}/chart —— lightweight-charts 数据（占位）
# ---------------------------------------------------------------------------
@router.get("/plans/{plan_id}/chart", summary="计划图表数据（lightweight-charts，Task 6 完善）")
async def get_chart_data(plan_id: str) -> Dict[str, Any]:
    """返回 lightweight-charts 渲染所需的计划图表数据。

    物理意图（Task 6 viz 层入口契约）：
        本端点当前为【占位实现】——返回 plan 的基本信息 + 占位 chart 字段，
        供前端先打通端到端调用链。Task 6 接入真实 viz 层后完善：
          - 末段 K 线序列（lightweight-charts candlestick data）；
          - 颈线/止损/止盈标注线（priceLine markers）；
          - 形态 pivot 标注（A/B/C/D 波位置 marker）。

    异常策略：plan_id 不存在 → 404（与 get_plan 一致的状态机守护）。
    """
    result = caisen_service.get_plan(plan_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"计划不存在：plan_id={plan_id!r}")
    # 占位：返回 plan 基本信息 + 待 Task 6 填充的 chart 结构
    return {
        "plan_id": result.plan_id,
        "symbol": result.symbol,
        "pattern_type": result.pattern_type,
        # 关键价位（前端先按这些画标注线占位）
        "breakout_price": result.breakout_price,
        "neckline_price": result.neckline_price,
        "bottom_price": result.bottom_price,
        "entry_upper": result.entry_upper,
        "entry_lower": result.entry_lower,
        "stop_loss": result.stop_loss,
        "take_profit": result.take_profit,
        "take_profit_2x": result.take_profit_2x,
        # 占位：Task 6 viz 层填充真实 K 线 + marker 数据
        "candles": [],
        "markers": [],
    }


# ---------------------------------------------------------------------------
# 端点 7：GET /positions —— 形态学持仓（占位）
# ---------------------------------------------------------------------------
@router.get("/positions", summary="形态学持仓（占位，后续富化 trading_service）")
async def get_positions() -> Dict[str, Any]:
    """返回形态学持仓列表。

    物理意图（占位端点）：
        本端点当前为【占位实现】——返回空 positions 列表。后续接入
        trading_service.get_positions 做形态学持仓富化：
          - 关联 ARMED/FILLED 态 plan_id（从 storage.load_active_plans 读）；
          - 实时盈亏（从交易网关查当前持仓市价）；
          - plan 字段（stop_loss/take_profit 用于持仓监控触发止盈止损）。

    Why 占位而非 501：前端先打通 /positions 端到端调用链（返 200 + 空列表），
    避免 501 阻塞 Phase 3 前端集成；后续富化是纯增量，契约不变。
    """
    return {"positions": []}


# ---------------------------------------------------------------------------
# 端点 8：POST /replay —— 历史回放
# ---------------------------------------------------------------------------
@router.post("/replay", summary="历史回放（胜率/盈亏比/回撤统计）")
async def replay(body: ReplayRequest) -> ReplayReportResponse:
    """回放：对 price_data 滚动执行 screener→plan→离场模拟，统计胜率/盈亏比/回撤。

    物理意图（蔡森流水线复盘节点）：
        前端 POST {start, end, universe, cfg_override} 触发历史区间回放，service 层
        调 backtest_replay.replay 统计 n_hits/win_rate/avg_rr/max_drawdown/...。

    异常策略：
        - ValidationError/ValueError/KeyError 透传 → 本层转 422/404；
        - 算法/IO 异常 service 已降级返零统计报告 → 本层返 200（非 500）。
        - 无 price_data（universe 空 / data_lake 未接）→ service 降级零统计 → 200。
    """
    try:
        return caisen_service.run_replay(body)
    except (KeyError, ValidationError, ValueError) as exc:
        raise _map_service_exception(exc)
