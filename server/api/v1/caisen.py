# -*- coding: utf-8 -*-
"""蔡森形态学流水线 REST 路由（Phase 3 · Task 4 + Task 6 chart 完善）。

物理定位（CLAEDE.md 极简 + 显式原则）：
    本模块是蔡森形态学流水线的"对外 HTTP 契约层"——把 caisen_service 的六个编排函数
    （run_scan / list_plans / approve_plan / activate_plan / get_plan / run_replay）
    + chart 端点（Task 6 接 viz_interactive）+ positions 占位端点封装为 REST 友好接口，
    前端/调度器只感知这一层。

    端点清单（prefix=/caisen，挂载时叠加 /api/v1 → /api/v1/caisen/...）：
        POST   /scan                     扫描（run_scan → list[CandidatePlan]）
        GET    /plans?status=            读盘 + status 过滤（list_plans）
        GET    /plans/{plan_id}          单计划查询（get_plan；KeyError→404）
        PATCH  /plans/{plan_id}          审核 approve/reject + edits（approve_plan）
        POST   /plans/{plan_id}/activate 激活 APPROVED→ARMED（activate_plan）
        GET    /plans/{plan_id}/chart    lightweight-charts 数据（Task 6：接 viz_interactive）
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
    - chart 端点 Task 6 完善：price_data 可装配（data_lake 接入后）→ build_chart_data
      返完整 candles+markers+priceLines；不可装配（当前 Phase 3+ 占位）→ priceLines-only 降级。
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
# 端点 6：GET /plans/{plan_id}/chart —— lightweight-charts 数据（Task 6 接 viz_interactive）
# ---------------------------------------------------------------------------
@router.get("/plans/{plan_id}/chart", summary="计划图表数据（lightweight-charts K线+标注）")
async def get_chart_data(plan_id: str) -> Dict[str, Any]:
    """返回 lightweight-charts 渲染所需的计划图表数据（candles + markers + priceLines）。

    物理意图（Task 6 viz 层入口契约）：
        1. 从 storage.get_plan 读原始 plan dict（含 metadata.pattern_points，标记用）；
        2. 尝试从 data_lake 装配 price_data（_load_price_data，生产 Phase 3+ 接入）：
           - 可装配 → viz_interactive.build_chart_data 返回完整 candles+markers+priceLines；
           - 不可装配 → 降级仅返 priceLines（从 plan 字段直接构造止损/止盈/颈线价位线），
             candles/markers 留空，前端仍能画关键价位（不白屏）。
        3. plan 基本信息字段（symbol/pattern_type/关键价位）一并附在顶层供前端快速渲染。

    异常策略：plan_id 不存在 → 404（与 get_plan 一致的状态机守护）。
    viz_interactive 装配异常 → 降级 priceLines-only，不抛 500（图缺失好过端点宕机）。
    """
    # storage.get_plan 返回原始 dict（含 metadata），caisen_service.get_plan 返回
    # CandidatePlan（无 metadata）——chart 端点需要 metadata.pattern_points 装配 markers，
    # 故直接走 storage 取 dict。
    from caisen import storage as _storage
    plan_dict = _storage.get_plan(plan_id)
    if plan_dict is None:
        raise HTTPException(status_code=404, detail=f"计划不存在：plan_id={plan_id!r}")

    # 基本信息字段（前端先按这些画占位 + 顶层快速访问）
    base_fields = {
        "plan_id": plan_dict.get("plan_id"),
        "symbol": plan_dict.get("symbol"),
        "pattern_type": plan_dict.get("pattern_type"),
        "breakout_price": plan_dict.get("breakout_price"),
        "neckline_price": plan_dict.get("neckline_price"),
        "bottom_price": plan_dict.get("bottom_price"),
        "entry_upper": plan_dict.get("entry_upper"),
        "entry_lower": plan_dict.get("entry_lower"),
        "stop_loss": plan_dict.get("stop_loss"),
        "take_profit": plan_dict.get("take_profit"),
        "take_profit_2x": plan_dict.get("take_profit_2x"),
    }

    # 尝试装配 price_data：生产 Phase 3+ 接 data_lake 后返回真实 OHLCV DataFrame；
    # 当前（Phase 3+ 未接）返回空 dict → 走 priceLines-only 降级路径。
    symbol = plan_dict.get("symbol")
    formed_at = plan_dict.get("formed_at")
    chart_payload: Dict[str, Any] = {"candles": [], "markers": [], "priceLines": []}
    try:
        price_data = caisen_service._load_price_data(
            [symbol] if symbol else None,
            str(formed_at) if formed_at is not None else "",
        )
        price_df = price_data.get(symbol) if isinstance(price_data, dict) else None
        if price_df is not None and not price_df.empty:
            # price_data 可装配 → 走完整 viz_interactive 装配
            from caisen.viz_interactive import build_chart_data
            chart_payload = build_chart_data(plan_dict, price_df)
        else:
            # price_data 不可装配 → 降级 priceLines-only（从 plan 字段直接构造）
            chart_payload = {"candles": [], "markers": [], "priceLines": _fallback_price_lines(plan_dict)}
    except Exception as exc:
        logger.warning(
            "chart 端点 viz 装配异常降级 priceLines-only（plan_id=%s）：type=%s detail=%s",
            plan_id, type(exc).__name__, exc,
        )
        chart_payload = {"candles": [], "markers": [], "priceLines": _fallback_price_lines(plan_dict)}

    return {**base_fields, **chart_payload}


def _fallback_price_lines(plan_dict: dict) -> list:
    """price_data 不可装配时，从 plan 字段直接构造 lightweight-charts priceLines。

    物理意图：data_lake 未接入或标的 price_data 缺失时，前端 CaisenScreenView 仍能
    画出止损/止盈/颈线/突破等关键价位水平线（无 K 线 + 形态点，但不白屏）。
    """
    lines = []
    spec = [
        ("take_profit_2x", "#009933", 1, 2, "第二波满足"),
        ("take_profit",    "#009933", 2, 0, "止盈·第一波满足"),
        ("breakout_price", "#0066cc", 1, 2, "突破价"),
        ("neckline_price", "#ff8800", 1, 0, "颈线"),
        ("bottom_price",   "#888888", 1, 2, "C波低点"),
        ("stop_loss",      "#cc0000", 2, 0, "止损"),
    ]
    for key, color, lw, ls, title in spec:
        v = plan_dict.get(key)
        if v is not None and not (isinstance(v, float) and v != v):  # NaN 守护
            lines.append({
                "price": float(v), "color": color,
                "lineWidth": lw, "lineStyle": ls,
                "axisLabelVisible": True, "title": title,
            })
    return lines


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
