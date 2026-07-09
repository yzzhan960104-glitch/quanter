# -*- coding: utf-8 -*-
"""蔡森形态学流水线 server 层 Pydantic 契约（Phase 3 · Task 3）。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本模块是蔡森流水线"对外契约层"——把 Phase 2 的纯函数算法（PatternScreener /
    TradePlanGenerator / backtest_replay）与 Phase 3 storage（JSON 文件持久化）
    的输出/输入封装为 REST 友好的 Pydantic 模型，供 Task 4 路由层 + 前端消费。

    设计红线：
        - 字段对齐 caisen.plan.TradePlan（Phase 2 值对象），无字段漂移；
        - 时间字段（formed_at/valid_until/max_holding_until）统一 ISO 字符串
          （JSON 原生可序列化，前端按需解析）；
        - status 枚举严格同源 storage 状态机：
          PENDING_APPROVAL → APPROVED → ARMED → FILLED → CLOSED（+ REJECTED）；
        - NaN 经 StrictJSONResponse 早抛（既有 server 约定，本契约层不重复清洗数值）。

蔡森方法学对齐：
    CandidatePlan 暴露的所有字段都是"已计算完成的快照"——前端只读消费，不做
    二次推导。盈亏比/止损位/满足点等数学内核已在 Phase 2 plan.py 完成，本层
    仅做契约封装，零业务逻辑（显式至上，拒绝过度封装）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 候选/计划响应（对齐 caisen.plan.TradePlan 全字段）
# ---------------------------------------------------------------------------
class CandidatePlan(BaseModel):
    """蔡森候选交易计划（响应体，字段对齐 caisen.plan.TradePlan）。

    物理意图：
        这是 server 层对"一个已生成的交易计划"的标准表示——前端表格直接渲染，
        REST 路由 list_plans/get_plan/approve_plan/activate_plan/run_scan 都返回它。

    字段对齐说明（与 TradePlan dataclass 一一对应）：
        plan_id / symbol / pattern_type / formed_at：标识 + 形态元信息；
        breakout_price / neckline_price / bottom_price：颈线/谷底关键价位；
        entry_upper / entry_lower：回踩挂单区间（突破价 ~ 突破价×(1-pullback)）；
        stop_loss：C 波低点止损（谷底 - buffer×ATR）；
        take_profit / take_profit_2x：颈线满足第一/第二波（等额累加 H）；
        rr_ratio：盈亏比（≥ min_rr_ratio 才会出现在响应中）；
        valid_until / max_holding_until：回踩触发窗口 / 时间止损截止日（ISO 字符串）；
        shares：分配股数（A 股整手，position_size 计算）；
        status：状态机当前态（storage 维护，初始 PENDING_APPROVAL）。

    注：TradePlan 的 H/timeout_exit_threshold/metadata 字段此处暂不暴露（H 是中间
    量、threshold 是 cfg 常量、metadata 是审计内部用）——保持前端契约精简，避免
    暴露实现细节导致前后端耦合。后续若需要可增量追加。
    """
    plan_id: str
    symbol: str
    pattern_type: str                                   # ∈ {"w_bottom", "head_shoulder"}
    formed_at: str                                      # ISO 字符串（Timestamp.isoformat）
    breakout_price: float
    neckline_price: float
    bottom_price: float
    entry_upper: float
    entry_lower: float
    stop_loss: float
    take_profit: float                                  # 第一波满足 = 颈线 + 1×H
    take_profit_2x: float                               # 第二波满足 = 颈线 + 2×H
    rr_ratio: float
    valid_until: str                                    # ISO 字符串
    max_holding_until: str                              # ISO 字符串
    shares: int
    status: str = "PENDING_APPROVAL"                    # 状态机当前态


# ---------------------------------------------------------------------------
# 扫描请求
# ---------------------------------------------------------------------------
class ScanRequest(BaseModel):
    """POST /caisen/scan 请求体：触发当日扫描。

    物理意图：
        date：       扫描交易日（用于 macro_position_coef + plans/<date>.json 文件名）；
        universe：   标的池（symbol 列表，生产由 data_lake 装配 price_data）；
        cfg_override：策略参数增量覆盖（如临时放宽 min_rr_ratio 做样本收集）。

    cfg_override 语义（与 service._merge_cfg 对齐）：
        dict 形如 {"min_rr_ratio": 1.5, "pullback_max_pct": 0.03}，
        经 StrategyConfig.model_copy(update=cfg_override) 增量合并到默认配置。
        空 dict = 用默认 StrategyConfig。
    """
    date: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="扫描交易日 YYYY-MM-DD（同时作 plans/<date>.json 文件名，严格格式防路径遍历 B-2）",
    )
    universe: List[str]
    cfg_override: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 计划审核（approve/reject + 微调）
# ---------------------------------------------------------------------------
class PlanReview(BaseModel):
    """POST /caisen/plans/{plan_id}/review 请求体：人工审核动作。

    物理意图：
        action： ∈ {"approve", "reject"}
            approve → status 推进到 APPROVED（可继续 activate 进入 ARMED）；
            reject  → status 推进到 REJECTED（不再进入挂单流程）。
        edits：  字段微调（如人工调整 stop_loss/take_profit）。
            实盘场景：风控官基于经验判断微调止损位/止盈位，覆盖算法默认值。
            仅在 action=approve 时有意义（reject 时忽略 edits）。

    防御性：edits 字段名必须与 CandidatePlan/storage plan dict 字段同名，
    service 层透传到 storage.update_plan(plan_id, **edits)，不在此处做白名单
    （路由层/service 层可按需加校验，当前保持极简）。
    """
    action: str                                         # "approve" / "reject"
    edits: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 回放请求
# ---------------------------------------------------------------------------
class ReplayRequest(BaseModel):
    """POST /caisen/replay 请求体：触发历史回放。

    物理意图：
        start / end：回放起止交易日（index label，整数 RangeIndex 或 ISO 日期）；
        universe：     回放标的池（symbol 列表，生产由 data_lake 装配 price_data）；
        cfg_override：策略参数增量覆盖（同 ScanRequest.cfg_override 语义）。

    回放语义（对齐 backtest_replay.replay）：
        对每个交易日 T 用【T 及之前】数据滚动跑 screener→plan→离场模拟，
        统计胜率/平均盈亏比/最大回撤/命中数/形态分布/月度收益。
        无前视红线：严格 .loc[:T] 裁剪。

    universe 契约（Task 3 review I-2）：
        Optional，默认 None = 全市场回放。当前 Phase 3 data_lake 全市场装配尚未
        接入，_load_price_data(None) 仍返回空 dict 占位（run_replay 降级零统计）。
        契约层入口先就位，Phase 3+ 接 data_lake 后生产全市场回放即生效；调用方
        亦可显式传 universe=[...] 缩小到指定标的池。
    """
    start: str
    end: str
    universe: Optional[List[str]] = None
    cfg_override: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 回放报告响应（对齐 caisen.backtest_replay.ReplayReport 全字段）
# ---------------------------------------------------------------------------
class ReplayReportResponse(BaseModel):
    """POST /caisen/replay 响应体：回放统计报告（字段对齐 ReplayReport）。

    字段物理意图（与 ReplayReport dataclass 一一对应）：
        n_hits：       命中（成交）交易笔数；
        win_rate：     胜率 = 盈利笔数 / n_hits（n_hits=0 时为 0.0）；
        avg_rr：       平均盈亏比；
        max_drawdown： 最大回撤（基于累计 rr 曲线，负值）；
        pattern_dist： 形态分布 {"w_bottom": x, "head_shoulder": y}；
        monthly_returns：月度收益（按 entry_date 月份聚合的 rr 之和）；
        avg_holding_bars：平均持仓天数；
        min_rr_ratio_recommendation：数据驱动的生产 min_rr_ratio 建议（中文）。

    注：ReplayReport.metadata（含完整 hits 列表 + cfg 快照）此处不暴露——
    前端展示只需聚合统计，完整 hits 列表体积大且含内部字段，按需单独接口提供。
    """
    n_hits: int
    win_rate: float
    avg_rr: float
    max_drawdown: float
    pattern_dist: Dict[str, int]
    monthly_returns: Dict[str, float]
    avg_holding_bars: float
    min_rr_ratio_recommendation: str
