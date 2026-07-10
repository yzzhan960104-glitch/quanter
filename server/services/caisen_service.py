# -*- coding: utf-8 -*-
"""蔡森形态学流水线 server 层编排服务（Phase 3 · Task 3）。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本模块是蔡森流水线的"对外编排层"——把 Phase 2 的纯函数算法
    （PatternScreener / TradePlanGenerator / backtest_replay）与 Phase 3 storage
    （Task 1 计划持久化）串接，对外暴露六个编排函数，供 Task 4 REST 路由调用：

        run_scan(req)           扫描：screener.screen → plan.generate → storage.save_plans
        list_plans(status)      读盘：storage.load_plans（可选 status 过滤）
        approve_plan(id, review) 审核：storage.update_plan（approve→APPROVED / reject→REJECTED）
        activate_plan(id)       激活：storage.update_plan(status=ARMED)
        get_plan(id)            查询：storage.get_plan
        run_replay(req)         回放：backtest_replay.replay

    编排红线（CLAUDE.md 量化风控·边界审查）：
        - service 层不做 HTTP 语义（不抛 HTTPException，路由层负责转译）；
        - 状态机异常（plan_id 不存在）抛 KeyError，路由层转 404；
        - 算法异常（screener/plan/replay 内部）不裸抛到路由层外——service 层 try/except
          捕获并降级返回空结果 + 日志（禁裸抛到路由层以外，杜绝 500 噪声污染前端）。

设计取舍（Why 这样切分）：
    - cfg_override 增量合并：StrategyConfig.model_copy(update=cfg_override)，
      浅拷贝默认配置后增量覆盖，零侵入（不修改全局默认 cfg 实例）。
    - price_data 装配：_load_price_data 走 DataLakeReader（lifespan 已 load daily 湖；
      独立进程无 lifespan 时自确保 load，守卫防重复）。universe 为 None/[] 时按
      reader.symbols 全市场枚举；reader 离线/湖空时降级返空 dict（测试可 monkeypatch 注入）。
    - amount 单位统一：data_lake amount 落盘为千元（tushare pro.daily 原生），_load_price_data
      装配时已 ×1000 转元，与 risk.liquidity_min_amount=1e8(元) 口径一致（#3）。

蔡森方法学对齐：
    server 层是蔡森流水线的"对外契约层"——把算法 + 持久化封装为 REST 友好的
    Pydantic 契约，前端/调度器只感知这一层。所有数学内核（颈线满足/盈亏比/C 波
    低点止损）已在 Phase 2 完成，本层零业务逻辑（显式至上，拒绝过度封装）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pandas as pd
from pydantic import ValidationError

from caisen import plan as plan_mod
from caisen import backtest_replay
from caisen import storage
from caisen.config import StrategyConfig
from caisen.patterns.screener import PatternScreener
from caisen.risk import RiskManager
from server.schemas.caisen import (
    CandidatePlan,
    PlanReview,
    ReplayReportResponse,
    ReplayRequest,
    ScanRequest,
)


# 模块级 logger：编排异常走 warning（不污染 prod 日志，但可调试追溯）
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内部辅助：默认 AUM / price_data 装配（生产走 data_lake，测试 monkeypatch 注入）
# ---------------------------------------------------------------------------
# 默认账户总资金（AUM）。生产应从交易网关 get_asset() 动态读取，此处用保守常量
# 占位——service 编排不阻塞于网关可达性（网关不可用时回退常量，保证扫描可用）。
# 实盘接入后应改为 risk.position_size(aum=gw_asset.total_asset, ...) 动态读取。
_DEFAULT_AUM: float = 1_000_000.0


def _load_price_data(symbols: Optional[List[str]], date: str) -> Dict[str, pd.DataFrame]:
    """按标的池 + 日期从 data_lake 装配 price_data（生产走 DataLakeReader）。

    物理意图：
        生产：DataLakeReader.get_instance()（lifespan 已 load daily 湖进内存），
              按 symbols 逐标的 get_timeseries 取时序并装配。
        universe 语义：symbols 为 None 或 [] → 全市场枚举（reader.symbols）。
        单位统一：data_lake amount 为千元（tushare pro.daily 原生），装配时 ×1000 转元，
              与 risk.liquidity_min_amount=1e8(元) 口径一致（#3）。
        离线降级：reader 未 load / 湖空 / 全部 symbol 取空 → 返 {}，run_scan/run_replay
              按既有契约降级（空候选 / 零统计），不抛异常。

    参数：
        symbols: 标的池（ScanRequest.universe / ReplayRequest.universe）。
                 None 或 [] → 全市场。
        date:    截止交易日（scan 传 T 日取 [:T] 无前视；replay 传 req.end 取全历史段）。

    返回：
        {symbol: DataFrame}（OHLCV + amount 已转元）。离线/空时返回 {}。
    """
    from data.lake_reader import DataLakeReader
    from config import LAKE_CONFIG

    reader = DataLakeReader.get_instance()
    lake = LAKE_CONFIG.get("default_lake") or "daily"
    # ensure daily 湖已 load：server lifespan 启动时 load，但独立进程（celery worker /
    # 脚本）无 lifespan，此处自确保（守卫防重复 load；首次 load 408MB 进内存）。
    import os
    if not reader.loaded or lake not in reader.lakes():
        daily_path = LAKE_CONFIG["lakes"].get(lake)
        if daily_path and os.path.exists(daily_path):
            reader.load(daily_path, key=lake)
    if not reader.loaded:
        logger.debug("_load_price_data 离线（daily 湖缺失/加载失败），返空 dict")
        return {}

    # universe 解析：None/空 → 全市场枚举
    if not symbols:
        symbols = reader.symbols(lake)
        if not symbols:
            logger.debug("_load_price_data 湖无 symbol（lake=%s），返空 dict", lake)
            return {}

    price_data: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        # start 用足够早的固定日期（早于 daily 湖起点 2016），get_timeseries 的
        # .loc[start:end] 闭区间切片自然截到实际数据范围；end=date 截到 T 日（无前视）。
        ts = reader.get_timeseries(sym, start="2010-01-01", end=date, lake=lake)
        if ts is None or ts.empty:
            continue
        # 列对齐（screener 需 close/high/low/volume/amount）
        cols = [c for c in ("open", "high", "low", "close", "volume", "amount")
                if c in ts.columns]
        ts = ts[cols].copy()
        # #3 单位统一：amount 千元 → 元（流动性过滤口径统一；volume 手单位不影响策略，
        # 放量校验全用比例 right_vol_shrink/breakout_vol_multiplier，单位无关）。
        if "amount" in ts.columns:
            ts["amount"] = ts["amount"] * 1000.0
        price_data[sym] = ts
    return price_data


def _merge_cfg(cfg_override: Dict[str, Any]) -> StrategyConfig:
    """默认 StrategyConfig + cfg_override 增量合并（extra=forbid 动态子类全字段校验）。

    物理意图：用户传入 cfg_override（如 {"min_rr_ratio": 1.5}）增量覆盖默认配置，
    不修改全局默认 cfg 实例（每次重新 model_validate 构造新对象）。

    防御性（Task 3 review I-1 校准）：
        Pydantic v2 有两个静默陷阱会掩盖非法 cfg_override：
          (1) `model_copy(update=...)` 是【不触发校验】的浅拷贝——未知字段名会被
              静默当作新属性附加，不抛 ValidationError；
          (2) `model_validate` 的默认 extra="ignore" 会静默丢弃未知字段名。
        二者都会让"cfg_override 字段名拼错"（如 {"min_rr_ration": 1.5}）被前端
        误当作"无候选"而非"参数错误"，掩盖配置 Bug。
        故此函数构建一个 extra="forbid" 的动态子类（_ForbidExtraConfig）做全字段
        校验——未知字段 / 类型不匹配 / 约束违反（ge/le）统一抛 ValidationError。
        service 层不静默吞，让其上抛路由层转 422（参数错误）。
    """
    base = StrategyConfig()
    if not cfg_override:
        return base
    # extra="forbid" 动态子类：Pydantic v2 默认 extra="ignore" 静默丢弃未知字段，
    # 故临时开 forbid 才能让拼错字段名 → ValidationError 透传路由层转 422。
    # 用 create_model 生成子类保持 StrategyConfig 全部字段/约束不变，仅叠加 forbid。
    from pydantic import ConfigDict, create_model
    _ForbidExtra = create_model(
        "_ForbidExtraConfig",
        __base__=StrategyConfig,
        __config__=ConfigDict(extra="forbid"),
    )
    merged: Dict[str, Any] = {**base.model_dump(), **cfg_override}
    return _ForbidExtra.model_validate(merged)


def _plan_to_candidate(d: Dict[str, Any]) -> CandidatePlan:
    """storage plan dict → CandidatePlan（Timestamp → ISO 字符串）。

    storage.load_plans 返回的 dict 中 formed_at/valid_until/max_holding_until
    已被 _restore_plan_dict 还原为 pd.Timestamp（见 caisen/storage.py），
    此处统一 isoformat 为字符串，对齐 CandidatePlan 契约（ISO 字符串）。

    防御性：Timestamp 转换失败（理论上不会，storage 已保证类型）时 fallback str()。
    """
    def _iso(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, pd.Timestamp):
            return v.isoformat()
        return str(v)

    return CandidatePlan(
        plan_id=str(d["plan_id"]),
        symbol=str(d["symbol"]),
        pattern_type=str(d["pattern_type"]),
        formed_at=_iso(d.get("formed_at")),
        breakout_price=float(d["breakout_price"]),
        neckline_price=float(d["neckline_price"]),
        bottom_price=float(d["bottom_price"]),
        entry_upper=float(d["entry_upper"]),
        entry_lower=float(d["entry_lower"]),
        stop_loss=float(d["stop_loss"]),
        take_profit=float(d["take_profit"]),
        take_profit_2x=float(d["take_profit_2x"]),
        rr_ratio=float(d["rr_ratio"]),
        valid_until=_iso(d.get("valid_until")),
        max_holding_until=_iso(d.get("max_holding_until")),
        shares=int(d["shares"]),
        status=str(d.get("status", "PENDING_APPROVAL")),
    )


# ---------------------------------------------------------------------------
# 公开编排接口
# ---------------------------------------------------------------------------
def run_scan(req: ScanRequest) -> List[CandidatePlan]:
    """扫描编排：screener.screen → plan.generate → storage.save_plans → 返回 CandidatePlan。

    物理意图（编排链路）：
        1. cfg_override 增量合并到默认 StrategyConfig（_merge_cfg）；
        2. 装配 price_data（_load_price_data，生产走 data_lake）；
        3. screener.screen(price_data, date) → 候选 DataFrame（空则直接返回 []）；
        4. plan.generate(candidates, cfg, risk, aum, date) → list[TradePlan]；
        5. storage.save_plans(date, plans) → 落 plans/<date>.json（默认 status=PENDING_APPROVAL）；
        6. 读回 storage.load_plans() → 转 CandidatePlan 返回（保证 status 字段持久化）。

    防御性（CLAUDE.md 量化风控·边界审查·Task 3 review I-1 校准）：
        - 参数/状态机异常（ValidationError / ValueError / KeyError）【透传上抛】，
          由路由层转 422（参数错误）/404（状态机未命中）——让前端能区分"参数错误"
          vs"无候选"，避免非法 cfg_override 被静默吞成空结果；
        - 算法/IO 异常（screener/plan/storage/data_lake 内部）不裸抛到路由层，
          try/except 捕获并降级返回空列表 + warning 日志（禁裸抛到路由层以外，
          杜绝 500 噪声污染前端）；
        - universe 为空 → 流向 _load_price_data 全市场枚举（scan_universe beat
          传 universe=[] 触发全市场扫描；reader 离线时 _load_price_data 返空 →
          candidates 为空 → 降级返 []，不抛异常）；
        - amount 单位已在 _load_price_data 装配时 ×1000 转元（data_lake 千元 → 元），
          与 risk.liquidity_min_amount=1e8(元) 口径一致（#3）；流动性过滤在 screener
          内部执行，本编排层不重复校验。

    参数：
        req: ScanRequest（date/universe/cfg_override）。

    返回：
        list[CandidatePlan]，落盘后的候选计划（含 status=PENDING_APPROVAL 初始态）。
        无候选或算法异常降级时返回空列表；参数/状态机异常上抛（不降级）。

    异常（透传路由层）：
        ValidationError: cfg_override 字段名/值非法（路由层转 422）。
        ValueError: 状态机参数非法（路由层转 422）。
        KeyError: 状态机键未命中（路由层转 404）。
    """
    # 【Task3】空 universe 不再早返：流向 _load_price_data 全市场枚举（scan_universe beat
    # 传 universe=[] 触发全市场扫描；reader 离线时 _load_price_data 返空 → 下面 candidates
    # 为空 → 降级返 []）。
    try:
        cfg = _merge_cfg(req.cfg_override)   # 可能抛 ValidationError（cfg_override 非法）
        risk = RiskManager(cfg)
        price_data = _load_price_data(req.universe, req.date)
        if not price_data:
            # price_data 装配为空（data_lake 未接 / 标的不存在）→ 无候选，安全降级
            logger.info("run_scan 无可用 price_data（date=%s, universe=%s），返回空列表",
                        req.date, req.universe)
            return []

        screener = PatternScreener(cfg, risk)
        candidates = screener.screen(price_data, req.date)
        if candidates.empty:
            logger.info("run_scan 无形态命中（date=%s, universe=%s）", req.date, req.universe)
            return []

        plans = plan_mod.generate(
            candidates, cfg, risk, aum=_DEFAULT_AUM, date=req.date,
        )
        if not plans:
            logger.info("run_scan 无计划通过盈亏比过滤（date=%s, universe=%s）",
                        req.date, req.universe)
            return []

        # 落盘（默认 status=PENDING_APPROVAL，详见 storage.save_plans）
        storage.save_plans(req.date, plans)

        # 读回（含 status 字段），转 CandidatePlan 返回
        loaded = storage.load_plans()
        # 仅返回本次扫描落盘的计划（按 plan_id 过滤，避免混入历史 plan）
        new_ids = {p.plan_id for p in plans}
        result = [_plan_to_candidate(d) for d in loaded if d.get("plan_id") in new_ids]
        return result
    except (ValidationError, ValueError, KeyError):
        # 参数/状态机异常透传路由层转 422/404（Task 3 review I-1）：
        # ValidationError = cfg_override 字段名/值非法；ValueError = 业务参数非法；
        # KeyError = 状态机键未命中。让前端能区分"参数错误"vs"无候选"。
        raise
    except Exception as exc:
        # 编排红线：仅算法/IO 异常降级返回空列表 + warning 日志
        # （screener/plan/storage/data_lake 内部异常，路由层据此返 200 空结果或 500）
        logger.warning("run_scan 算法异常降级（date=%s）：type=%s detail=%s",
                       req.date, type(exc).__name__, exc, exc_info=True)
        return []


def list_plans(status: Optional[str] = None) -> List[CandidatePlan]:
    """读盘编排：storage.load_plans(status) → list[CandidatePlan]。

    参数：
        status: 状态过滤（None=全部；"APPROVED"/"ARMED"/... 精确过滤）。

    返回：
        list[CandidatePlan]。无 plans 文件时返回空列表（不抛异常）。
    """
    loaded = storage.load_plans(status=status)
    return [_plan_to_candidate(d) for d in loaded]


def approve_plan(plan_id: str, review: PlanReview) -> CandidatePlan:
    """审核编排：根据 review.action 推进 status + 应用 edits 微调。

    物理意图（蔡森流水线审核节点）：
        action=approve → status=APPROVED（进入可激活态）；
        action=reject  → status=REJECTED（不再进入 ARMED 流程）；
        edits          → 任意字段增量（如人工调整 stop_loss/take_profit）。

    状态机：PENDING_APPROVAL → (approve) APPROVED → (activate) ARMED → FILLED → CLOSED
                       └→ (reject) REJECTED

    参数：
        plan_id: 计划唯一标识。
        review:  PlanReview（action + edits）。

    返回：
        CandidatePlan（更新后的计划，含新 status + edits 字段）。

    异常：
        KeyError: plan_id 不存在（storage.update_plan 抛，路由层转 404）。
        ValueError: review.action 非法（非 approve/reject，路由层转 422）。
    """
    action = (review.action or "").strip().lower()
    if action == "approve":
        new_status = "APPROVED"
    elif action == "reject":
        new_status = "REJECTED"
    else:
        raise ValueError(f"approve_plan 非法 action={review.action!r}（合法值：approve/reject）")

    # storage.update_plan 不存在 plan_id 抛 KeyError（路由层转 404）
    fields: Dict[str, Any] = {"status": new_status}
    if action == "approve" and review.edits:
        # 仅 approve 时应用 edits（reject 时忽略微调，保持极简）
        fields.update(review.edits)
    storage.update_plan(plan_id, **fields)

    updated = storage.get_plan(plan_id)
    if updated is None:
        # 防御性：update 成功但 get 失败（理论上不会，文件竞态兜底）
        raise KeyError(f"approve_plan: plan_id={plan_id!r} 更新后读取失败")
    return _plan_to_candidate(updated)


def activate_plan(plan_id: str) -> CandidatePlan:
    """激活编排：status=ARMED（挂单待执行，同步进 active.json）。

    物理意图：
        APPROVED → ARMED 是"从审核通过到挂单待执行"的迁移。storage.update_plan 在
        status 进入 ARMED/FILLED 时自动同步进 active.json（执行器高频读路径）。

    参数：
        plan_id: 计划唯一标识（应为 APPROVED 态，本编排不做前置态校验——允许从
                 任意态直接 ARMED，灵活于调度器异常恢复场景；严格前置态校验留待
                 路由层/状态机守护层按需追加）。

    返回：
        CandidatePlan（更新后的计划，status=ARMED）。

    异常：
        KeyError: plan_id 不存在（路由层转 404）。
    """
    storage.update_plan(plan_id, status="ARMED")
    updated = storage.get_plan(plan_id)
    if updated is None:
        raise KeyError(f"activate_plan: plan_id={plan_id!r} 更新后读取失败")
    return _plan_to_candidate(updated)


def get_plan(plan_id: str) -> Optional[CandidatePlan]:
    """查询编排：storage.get_plan → CandidatePlan（未命中 None，路由层转 404）。

    参数：
        plan_id: 计划唯一标识。

    返回：
        CandidatePlan 或 None（未命中）。
    """
    d = storage.get_plan(plan_id)
    if d is None:
        return None
    return _plan_to_candidate(d)


def run_replay(req: ReplayRequest) -> ReplayReportResponse:
    """回放编排：backtest_replay.replay → ReplayReportResponse。

    物理意图：
        对 price_data 滚动执行 screener→plan→离场模拟，统计胜率/平均盈亏比/最大回撤/
        命中数/形态分布/月度收益。无前视红线：严格 .loc[:T] 裁剪（详见 backtest_replay）。

    参数：
        req: ReplayRequest（start/end/universe/cfg_override）。
            universe=None/[] → 全市场回放（_load_price_data 按 reader.symbols 枚举；
            reader 离线时降级返空 → 零统计报告）；universe=[...] 缩小到指定标的池
            （_load_price_data 按 symbols 装配）。

    返回：
        ReplayReportResponse（字段对齐 ReplayReport，metadata 内部字段不暴露）。

    防御性（Task 3 review I-1 + I-2 校准）：
        - 参数/状态机异常（ValidationError / ValueError / KeyError）【透传上抛】，
          由路由层转 422/404——让前端能区分"参数错误"vs"无样本"；
        - 算法/IO 异常不裸抛到路由层——try/except 捕获并降级返回零统计报告
          （n_hits=0/win_rate=0.0/...，杜绝 500 噪声）；
        - price_data 装配为空 → 同样降级返回零统计（无样本可回放）。

    异常（透传路由层）：
        ValidationError: cfg_override 字段名/值非法（路由层转 422）。
        ValueError: 业务参数非法（路由层转 422）。
        KeyError: 状态机键未命中（路由层转 404）。
    """
    try:
        cfg = _merge_cfg(req.cfg_override)   # 可能抛 ValidationError（cfg_override 非法）
        risk = RiskManager(cfg)
        # price_data 装配（Task 3 review I-2）：
        #   req.universe 由 ReplayRequest 契约层传入：None/[] = 全市场枚举
        #   （reader.symbols 列举全 symbol；reader 离线时 _load_price_data 返空降级零统计）；
        #   显式 universe=[...] 缩小到指定标的池。
        #   _load_price_data 收到 None/[] 时内部按全市场装配（reader 走 default_lake，
        #   lifespan 或自确保已 load；测试可 monkeypatch 注入合成数据）。
        universe = req.universe if req.universe is not None else []
        # 【Task3】传 req.end（取 [start,end] 全段），而非 req.start（[:start] 数据不足）；
        # backtest_replay.replay 内部按 T∈[start,end] 滚动 .loc[:T] 隔离未来（无前视）。
        price_data = _load_price_data(universe, req.end)
        if not price_data:
            logger.info("run_replay 无可用 price_data（start=%s, end=%s, universe=%s），返回零统计",
                        req.start, req.end, req.universe)
            return _empty_replay_report()

        report = backtest_replay.replay(
            price_data, cfg, risk,
            start=req.start, end=req.end, aum=_DEFAULT_AUM,
        )
        return ReplayReportResponse(
            n_hits=report.n_hits,
            win_rate=report.win_rate,
            avg_rr=report.avg_rr,
            max_drawdown=report.max_drawdown,
            pattern_dist=report.pattern_dist,
            monthly_returns=report.monthly_returns,
            avg_holding_bars=report.avg_holding_bars,
            min_rr_ratio_recommendation=report.min_rr_ratio_recommendation,
        )
    except (ValidationError, ValueError, KeyError):
        # 参数/状态机异常透传路由层转 422/404（Task 3 review I-1）：
        # 让前端能区分"参数错误"vs"无样本"，避免非法 cfg_override 被静默吞成零统计。
        raise
    except Exception as exc:
        # 编排红线：仅算法/IO 异常降级返回零统计（路由层据此返 200 或 500）
        logger.warning("run_replay 算法异常降级（start=%s, end=%s）：type=%s detail=%s",
                       req.start, req.end, type(exc).__name__, exc, exc_info=True)
        return _empty_replay_report()


def _empty_replay_report() -> ReplayReportResponse:
    """构造零统计回放报告（降级用，n_hits=0/win_rate=0.0/...）。

    物理意图：回放无样本（price_data 空）或异常时，返回结构完整的零报告而非抛错，
    保证前端拿到合法响应体（n_hits=0 即可提示"无样本"，不阻断 UI）。
    """
    return ReplayReportResponse(
        n_hits=0,
        win_rate=0.0,
        avg_rr=0.0,
        max_drawdown=0.0,
        pattern_dist={},
        monthly_returns={},
        avg_holding_bars=0.0,
        min_rr_ratio_recommendation=(
            "回放无样本：price_data 装配为空或异常降级。请确认 data_lake 已接入 / "
            "cfg_override 参数合法。"
        ),
    )
