# -*- coding: utf-8 -*-
"""蔡森形态学流水线应用门面（facade）——模型层对外的唯一契约（Step 2.1）。

物理定位（design §6.3 strangler 搬运）：
    本模块是 caisen 模型层的「对外门面」——收口 caisen 内部全部 8 处穿透符号
    （plan_mod / backtest_replay / replay_runs / replay_tasks_db / storage /
    StrategyConfig / PatternScreener / RiskManager），对 server 层（路由 + service）
    暴露 10 个公开用例 + 4 个内部辅助。

    核心价值（为什么需要 facade）：
        后续 caisen 内部分包重组（Task 3：patterns/engines/optimize/infra 子包拆分）
        对 server 层【完全不可见】——server 只 import caisen.facade.CaisenFacade，
        caisen 内部如何搬文件、改 import 路径，facade 内部消化，server 零改动。

搬运纪律（strangler 红线·逻辑零改动）：
    本文件方法体【逐行原样搬入】自 server/services/caisen_service.py（Phase 3 · Task 3
    的 14 个函数/辅助），仅做两类机械改写：
        ① 模块级函数 def f(args): → 实例方法 def f(self, args):
        ② 函数内对同级辅助的裸调用加 self.：
              _merge_cfg(...)         → self._merge_cfg(...)
              _load_price_data(...)   → self._load_price_data(...)
              _plan_to_candidate(...) → self._plan_to_candidate(...)
              _empty_replay_report()  → self._empty_replay_report()
    算法 / 参数 / 异常处理 / 降级逻辑 / 注释【一字不改】。

异常透传契约（design §6.3 钉死，facade 不得吞）：
    scan / replay 内 try 块的 `except (ValidationError, ValueError, KeyError): raise`
    原样保留——这三个异常透传路由层转 422（参数错误）/ 404（状态机未命中），
    让前端能区分「参数错误」vs「无候选 / 无样本」。算法 / IO 异常的降级
    （返空列表 / 零统计 + warning）同样原样保留。

注：caisen_service.py 旧函数【本 Task 不动】（Task 2.2 才把 service 降级为 facade 薄壳），
    当前 facade 是逻辑副本，新旧两套并存，行为完全一致。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pandas as pd
from pydantic import ValidationError

# ── caisen 内部 8 处穿透符号（facade 收口，server 不再直接 import 这些）──
from caisen import plan as plan_mod
from caisen import backtest_replay
from strategies.caisen_pattern import CaisenPatternStrategy  # 阶段A：策略适配器（回测引擎解耦）
from caisen import replay_runs
from caisen import replay_tasks_db
from caisen import storage
from caisen.config import StrategyConfig
from caisen.patterns.screener import PatternScreener
from caisen.risk import RiskManager
# ── data / config 横切依赖（非 caisen 内部，一并搬入 facade 保持行为一致）──
from data import symbol_names
# Step4e：_load_price_data / _merge_cfg 纯逻辑抽到 data/price_loader.py 模块级函数
# （二者不使用 self 状态）。facade 保留实例方法转发（兼容 self._load_price_data /
# self._merge_cfg 调用点），逻辑单源在 data.price_loader，消除双份真理。
from data.price_loader import load_price_data as _load_price_data_fn
from data.price_loader import merge_cfg as _merge_cfg_fn
from server.schemas.caisen import (
    CandidatePlan,
    PlanReview,
    ReplayReportResponse,
    ReplayRequest,
    ReplayRunDetail,
    ReplayRunSummary,
    ScanRequest,
)


# 模块级 logger：编排异常走 warning（不污染 prod 日志，但可调试追溯）
logger = logging.getLogger("caisen.facade")


class CaisenFacade:
    """蔡森形态学流水线应用门面（10 公开用例 + 4 内部辅助）。

    所有方法体逐行原样搬自 server/services/caisen_service.py，仅做 def→method +
    辅助调用加 self. 的机械改写，逻辑零改动。详细物理意图见各方法 docstring。
    """

    # 默认账户总资金（AUM）。生产应从交易网关 get_asset() 动态读取，此处用保守常量
    # 占位——facade 编排不阻塞于网关可达性（网关不可用时回退常量，保证扫描可用）。
    # 实盘接入后应改为 risk.position_size(aum=gw_asset.total_asset, ...) 动态读取。
    _DEFAULT_AUM: float = 1_000_000.0

    # -----------------------------------------------------------------------
    # 内部辅助：price_data 装配 / cfg 合并 / dict→CandidatePlan / 零统计报告
    # -----------------------------------------------------------------------
    # Step4e：_load_price_data / _merge_cfg 的【纯逻辑】已抽到 data/price_loader.py 模块级
    # 函数（二者不使用 self 状态，可纯模块级）。facade 保留实例方法转发（兼容 facade
    # 内部 self._load_price_data / self._merge_cfg 调用点 + 任何外部 facade 实例调用），
    # 消除双份真理——逻辑单源在 data.price_loader，本层仅转发。
    # （_load_price_data_fn / _merge_cfg_fn 在模块顶 import，见本文件顶部。）

    def _load_price_data(self, symbols: Optional[List[str]], date: str) -> Dict[str, pd.DataFrame]:
        """按标的池 + 日期从 data_lake 装配 price_data（转发 data.price_loader.load_price_data）。

        逻辑真身已 Step4e 抽到 data/price_loader.py（消除 execution/replay_worker 反向依赖
        caisen_service 的过渡债）。本方法仅转发，签名/行为/降级逻辑一字不改，详见
        data.price_loader.load_price_data 的 docstring。
        """
        return _load_price_data_fn(symbols, date)

    def _merge_cfg(self, cfg_override: Dict[str, Any]) -> StrategyConfig:
        """默认 StrategyConfig + cfg_override 增量合并（转发 data.price_loader.merge_cfg）。

        逻辑真身已 Step4e 抽到 data/price_loader.py。本方法仅转发，extra="forbid" 全字段
        校验 + ValidationError 透传路由层转 422 的契约不变，详见
        data.price_loader.merge_cfg 的 docstring。
        """
        return _merge_cfg_fn(cfg_override)

    def _plan_to_candidate(self, d: Dict[str, Any]) -> CandidatePlan:
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
            symbol_name=symbol_names.get_name(str(d["symbol"])),
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

    def _empty_replay_report(self) -> ReplayReportResponse:
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
            equity_curve=[],
            trades=[],
            annualized_return=0.0,
            n_trading_days=0,
        )

    # -----------------------------------------------------------------------
    # 公开编排接口（10 用例，方法体逐行原样搬自 caisen_service.py）
    # -----------------------------------------------------------------------
    def scan(self, req: ScanRequest) -> List[CandidatePlan]:
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
            cfg = self._merge_cfg(req.cfg_override)   # 可能抛 ValidationError（cfg_override 非法）
            risk = RiskManager(cfg)
            price_data = self._load_price_data(req.universe, req.date)
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
                candidates, cfg, risk, aum=self._DEFAULT_AUM, date=req.date,
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
            result = [self._plan_to_candidate(d) for d in loaded if d.get("plan_id") in new_ids]
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

    def list_plans(self, status: Optional[str] = None) -> List[CandidatePlan]:
        """读盘编排：storage.load_plans(status) → list[CandidatePlan]。

        参数：
            status: 状态过滤（None=全部；"APPROVED"/"ARMED"/... 精确过滤）。

        返回：
            list[CandidatePlan]。无 plans 文件时返回空列表（不抛异常）。
        """
        loaded = storage.load_plans(status=status)
        return [self._plan_to_candidate(d) for d in loaded]

    def approve_plan(self, plan_id: str, review: PlanReview) -> CandidatePlan:
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
        return self._plan_to_candidate(updated)

    def activate_plan(self, plan_id: str) -> CandidatePlan:
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
        return self._plan_to_candidate(updated)

    def get_plan(self, plan_id: str) -> Optional[CandidatePlan]:
        """查询编排：storage.get_plan → CandidatePlan（未命中 None，路由层转 404）。

        参数：
            plan_id: 计划唯一标识。

        返回：
            CandidatePlan 或 None（未命中）。
        """
        d = storage.get_plan(plan_id)
        if d is None:
            return None
        return self._plan_to_candidate(d)

    def replay(self, req: ReplayRequest) -> ReplayReportResponse:
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
            cfg = self._merge_cfg(req.cfg_override)   # 可能抛 ValidationError（cfg_override 非法）
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
            price_data = self._load_price_data(universe, req.end)
            if not price_data:
                logger.info("run_replay 无可用 price_data（start=%s, end=%s, universe=%s），返回零统计",
                            req.start, req.end, req.universe)
                return self._empty_replay_report()

            # 阶段A：策略解耦——构造 CaisenPatternStrategy 注入 replay（行为零变化）
            strategy = CaisenPatternStrategy(cfg, risk, self._DEFAULT_AUM)
            report = backtest_replay.replay(
                price_data, strategy,
                start=req.start, end=req.end,
            )
            response = ReplayReportResponse(
                n_hits=report.n_hits,
                win_rate=report.win_rate,
                avg_rr=report.avg_rr,
                max_drawdown=report.max_drawdown,
                pattern_dist=report.pattern_dist,
                monthly_returns=report.monthly_returns,
                avg_holding_bars=report.avg_holding_bars,
                min_rr_ratio_recommendation=report.min_rr_ratio_recommendation,
                equity_curve=report.equity_curve,
                trades=report.trades,
                annualized_return=report.annualized_return,
                n_trading_days=report.n_trading_days,
            )
            # 方案 A（用户决策）：save 默认 True=都存。落盘历史 + 回填 run_id（前端据此
            # 显示「已保存」+ 进历史列表）。落盘异常降级 run_id=None——持久化是附属价值，
            # 回放统计是主价值，IO 故障不应让回放 500（CLAUDE.md 量化风控·边界审查）。
            if req.save:
                try:
                    response.run_id = replay_runs.save_run(
                        req.model_dump(), response.model_dump(),
                    )
                except Exception as exc:
                    logger.warning(
                        "run_replay 落盘历史失败（降级 run_id=None）：type=%s detail=%s",
                        type(exc).__name__, exc, exc_info=True,
                    )
            return response
        except (ValidationError, ValueError, KeyError):
            # 参数/状态机异常透传路由层转 422/404（Task 3 review I-1）：
            # 让前端能区分"参数错误"vs"无样本"，避免非法 cfg_override 被静默吞成零统计。
            raise
        except Exception as exc:
            # 编排红线：仅算法/IO 异常降级返回零统计（路由层据此返 200 或 500）
            logger.warning("run_replay 算法异常降级（start=%s, end=%s）：type=%s detail=%s",
                           req.start, req.end, type(exc).__name__, exc, exc_info=True)
            return self._empty_replay_report()

    def replay_async(self, req) -> str:
        """提交异步回测：写 PENDING 行，立即返回 task_id（不阻塞）。

        物理意图（Spec 1 闭环地基）：全市场回测耗时几十分钟~几小时，同步 HTTP 必超时。
        本函数只写任务行（PENDING）立即返回，调度器 daemon 线程后续 poll 到该任务 →
        submit worker 子进程跑 replay → 写回 SUCCESS/FAILED/CANCELLED。进度/取消经回调
        + SQLite 全程可观测。

        参数：
            req：ReplayRequest（或任意含 model_dump 的对象；字段 start/end/universe/cfg_override）。
        返回：
            task_id（前端据此轮询 GET /replay/tasks/{task_id} 观测进度与结果）。
        """
        replay_tasks_db.init_db()               # 幂等建表（lifespan 亦建，重复无害）
        return replay_tasks_db.create_task(req.model_dump())

    def list_replay_runs(self) -> List[ReplayRunSummary]:
        """读 replay_runs/index.json → List[ReplayRunSummary]（按 created_at 降序）。

        物理意图：前端「历史回测记录」面板的列表数据源。storage 层 list_runs 返摘要
        dict 列表（不含完整 trades/equity_curve，保持轻量），本层套 Pydantic 契约对齐。

        防御性：index 单条损坏（字段缺失/类型错）→ 跳过该条 + warning（不抛，幂等读契约，
        与 load_plans 同源——index 文件可能被人工误编辑/写入损坏，不应让整个列表 500）。
        """
        result: List[ReplayRunSummary] = []
        for s in replay_runs.list_runs():
            if not isinstance(s, dict):
                continue
            try:
                result.append(ReplayRunSummary(**s))
            except Exception as exc:
                logger.warning("list_replay_runs 跳过损坏摘要（%s）：%s", exc, s)
        return result

    def get_replay_run(self, run_id: str) -> Optional[ReplayRunDetail]:
        """读 replay_runs/<run_id>.json → ReplayRunDetail（summary + report + request）。

        返回：完整详情；run_id 不存在或非法（路径遍历）→ None（路由层转 404）。
        防御性：记录文件损坏（report/summary 字段错）→ 返 None（路由层转 404）而非 500，
        与 get_plan 的 None 契约一致——前端按「记录不存在」处理。
        """
        payload = replay_runs.get_run(run_id)
        if payload is None:
            return None
        try:
            return ReplayRunDetail(
                summary=ReplayRunSummary(**payload["summary"]),
                report=ReplayReportResponse(**payload["report"]),
                request=payload.get("request", {}),
            )
        except Exception as exc:
            logger.warning(
                "get_replay_run 记录损坏（run_id=%s，转 None→404）：%s", run_id, exc,
            )
            return None

    def delete_replay_run(self, run_id: str) -> bool:
        """删 replay_runs/<run_id>.json + 从 index 移除。

        返回：True=删除成功；False=run_id 不存在或非法（路由层转 404）。
        透传 replay_runs.delete_run（它内部已处理路径遍历防御 + 锁内 RMW）。
        """
        return replay_runs.delete_run(run_id)
