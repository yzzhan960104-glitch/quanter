# -*- coding: utf-8 -*-
"""server.services.caisen_service 编排测试（蔡森形态学流水线 Phase 3 · Task 3）。

物理意图与覆盖节点（CLAUDE.md 量化风控·边界审查）：
  本测试验证蔡森形态学流水线 Phase 3 的"server 层编排服务"——把 Phase 2 算法
  （PatternScreener / TradePlanGenerator / backtest_replay）与 Phase 3 storage
  （Task 1 计划持久化）串接，对外暴露 run_scan / list_plans / approve_plan /
  activate_plan / get_plan / run_replay 六个编排函数，供 Task 4 REST 路由调用。

  覆盖节点：
    1. test_run_scan_persists_and_returns_plans：合成 price_data（含标准 W 底）
       → run_scan 落 storage + 返回 CandidatePlan 列表（字段对齐 TradePlan）；
    2. test_list_plans_filter_by_status：list_plans(status=...) 按 status 过滤；
    3. test_approve_plan_status_transition：approve_plan 推进 PENDING_APPROVAL→APPROVED；
    4. test_activate_plan_sets_armed：activate_plan 推进 APPROVED→ARMED；
    5. test_run_replay_returns_report：run_replay 返回 ReplayReportResponse（字段对齐 ReplayReport）。

设计要点（CLAUDE.md 极简 + 显式原则）：
  - 全程用 tmp_path fixture 隔离 storage（monkeypatch storage._PLANS_DIR），绝不污染真实 plans/；
  - 合成 price_data 复用 Task 8/10 已验证的标准 W 底序列（_build_w_bottom_with_rise 同源），
    保证 screener.screen 能产出非空候选 DataFrame，plan.generate 能产出 rr≥min_rr 的计划；
  - cfg_override 用宽松 min_rr_ratio=1.0（Bug4 修复后新 rr 公式下标准 W 底 rr≈1.4，
    生产默认 3.0 会过滤掉所有样本；min_rr_ratio 定标是独立 Phase3+ 待办）；
  - 服务编排要求"异常捕获返回结构化错误"（禁裸抛到路由层外）——测试用 monkeypatch
    注入异常验证降级路径。

蔡森方法学对齐：
  server 层是蔡森流水线的"对外契约层"——把 Phase 2 的纯函数算法 + Phase 3 的
  JSON 文件持久化封装为 REST 友好的 Pydantic 契约，前端/调度器只感知这一层。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from typing import Any, Dict

from caisen import storage
from caisen.config import StrategyConfig
from pydantic import ValidationError
from server.schemas.caisen import (
    CandidatePlan,
    PlanReview,
    ReplayRequest,
    ReplayReportResponse,
    ScanRequest,
)
from server.services import caisen_service


# ---------------------------------------------------------------------------
# 合成序列构造（复用 Task 8/10 标准 W 底序列，保证 screener 能命中）
# ---------------------------------------------------------------------------
def _mk_cfg(**overrides) -> StrategyConfig:
    """构造 service 测试用 StrategyConfig（宽松阈值，保证标准 W 底通过）。

    承 Task 9/10 rr 张力 + Bug4 修复后新 rr 公式：标准 W 底（止损远 + 回踩浅）
    新公式 rr≈1.4（如 breakout≈颈线时），min_rr_ratio 用 1.0 保证链路命中样本
    （生产默认 3.0 会过滤掉所有标准突破入场计划——min_rr_ratio 数据驱动定标是
    独立 Phase3+ 待办，非本链路测试范围）。
    """
    base = dict(
        min_pattern_bars=11,
        max_pattern_bars=60,
        zigzag_threshold_atr=0.5,
        confirm_bars=2,
        w_price_tolerance=0.05,
        min_pattern_depth=0.05,
        max_pattern_depth=0.50,
        hs_max_pattern_depth=1.0,
        pattern_tension_ratio=0.05,
        right_vol_shrink=0.8,
        breakout_vol_multiplier=1.5,
        right_above_left=True,
        ma26w_filter=False,
        abc_wave_detect=False,
        liquidity_min_amount=1e8,
        hv_window=20,
        hv_max_quantile=0.95,
        min_rr_ratio=1.0,
        pullback_window_bars=3,
        max_holding_bars=15,
        timeout_exit_threshold=0.01,
    )
    base.update(overrides)
    return StrategyConfig(**base)


# ScanRequest/ReplayRequest 的 cfg_override 用同源宽松 dict（service._merge_cfg 增量合并）。
# 物理意图：生产环境默认 StrategyConfig() 严格（confirm_bars=3/w_price_tolerance=0.02/
# ma26w_filter=True/abc_wave_detect=True 等），合成标准 W 底在严格默认下会被否决。
# 测试需传完整宽松 override 才能复现 Task 8/10 已验证的命中场景。
_LOOSE_CFG_OVERRIDE: Dict[str, Any] = dict(
    min_pattern_bars=11,
    max_pattern_bars=60,
    zigzag_threshold_atr=0.5,
    confirm_bars=2,
    w_price_tolerance=0.05,
    min_pattern_depth=0.05,
    max_pattern_depth=0.50,
    hs_max_pattern_depth=1.0,
    pattern_tension_ratio=0.05,
    right_vol_shrink=0.8,
    breakout_vol_multiplier=1.5,
    right_above_left=True,
    ma26w_filter=False,
    abc_wave_detect=False,
    liquidity_min_amount=1e8,
    hv_window=20,
    hv_max_quantile=0.95,
    min_rr_ratio=1.0,
    pullback_window_bars=3,
    max_holding_bars=15,
    timeout_exit_threshold=0.01,
)


def _w_vol_pattern(n: int, p1_i: int, p2_i: int, p3_i: int, p4_i: int) -> pd.Series:
    """W 底量价模式（同 Task 8/10）：左底放量 + 右底缩量 + 突破放量。"""
    vol = pd.Series(200.0, index=pd.RangeIndex(n))
    vol.iloc[p1_i] = 300.0   # 左底放量
    vol.iloc[p3_i] = 100.0   # 右底缩量
    vol.iloc[p4_i] = 500.0   # 突破日放量
    return vol


def _build_standard_w_bottom_price_df() -> pd.DataFrame:
    """合成标准 W 底 + 满足涨幅段的 OHLCV DataFrame（复用 Task 8/10 已验证序列）。

    前段 20 根：标准 W 底（左脚 7.5、右脚 8.0 抬高、颈线≈11、末根突破颈线）；
    后段：回踩 + 单边上涨（触发回踩买入 + 满足止盈）。

    关键（W 底检测窗口）：screener 用 causal_pivots 的 confirm_bars 隔离末段 pivot，
    故形态在"末根之前 confirm_bars 根"被确认。run_scan 在生产语义下调用的 price_data
    是【data_lake 在 T 日提供的 df.loc[:T]】——即末根就是 T 日本身。本合成函数返回
    完整序列（前段 + 后段），调用方按需用 .loc[:T] 截取喂 run_scan，使末根恰好落在
    形态确认点（T=19 即可命中，详见 _build_detections_w_bottom）。

    返回 DataFrame 含 close/high/low/volume/amount 列，index=RangeIndex，
    契约对齐 PatternScreener.screen 的输入要求。
    """
    pre_close = pd.Series(
        [12.0, 11.0, 10.0, 9.0, 8.0, 7.5,
         8.0, 8.5, 9.0, 10.0, 11.0,
         10.0, 9.0, 8.0,
         9.0, 10.0, 11.0, 13.0,
         12.5, 12.0],
        dtype=float,
    )
    neck = 11.0
    target = neck * 1.5
    pullback_price = 12.0 * 0.99
    pullback_seg = [pullback_price, pullback_price - 0.1]
    n_tail = 18
    rise_seg = np.linspace(pullback_seg[-1], target, n_tail - 2).tolist()
    tail_close = pullback_seg + rise_seg
    close = pd.concat([pre_close, pd.Series(tail_close, dtype=float)], ignore_index=True)

    n = len(close)
    # high/low 在 close 上下浮动 0.3（同 Task 8/10，screener 需 high/low 做 ATR/ZigZag）
    high = close + 0.3
    low = close - 0.3
    # volume：复用 Task 8/10 _w_vol_pattern（p1_i=5 左底 / p3_i=13 右底 / p4_i=17 突破）
    vol = _w_vol_pattern(n, p1_i=5, p2_i=10, p3_i=13, p4_i=17)
    tail_vol = pd.Series(250.0, index=pd.RangeIndex(len(pre_close), n))
    vol.iloc[len(pre_close):] = tail_vol.values
    # amount 取常数 2e8（≥ liquidity_min_amount=1e8 通过流动性过滤，同 Task 8/10 _mk_price_df）
    amount = pd.Series(2e8, index=pd.RangeIndex(n), dtype=float)

    df = pd.DataFrame({
        "close": close.values,
        "high": high.values,
        "low": low.values,
        "volume": vol.values,
        "amount": amount.values,
    }, index=pd.RangeIndex(n))
    return df


def _build_detections_w_bottom() -> pd.DataFrame:
    """截取合成序列到形态确认点（T=19），保证 run_scan 单次调用即命中。

    生产语义：run_scan(date=T) 调用时，data_lake 提供 df.loc[:T]（末根=T 日）。
    测试模拟此语义——把 _build_standard_w_bottom_price_df 的完整序列截取到 T=19
    （confirm_bars=2 后恰好命中 W 底突破），作为 price_data 喂 run_scan。

    返回截断后的 DataFrame（index 0..19，共 20 根）。
    """
    full = _build_standard_w_bottom_price_df()
    return full.loc[:19].copy()


# ---------------------------------------------------------------------------
# 公共 fixture：隔离 storage + 注入合成配置
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
    """每个测试自动隔离：storage._PLANS_DIR 指向 tmp_path/plans（绝不污染真实 plans/）。

    防御性（CLAUDE.md 量化风控·边界审查）：测试落盘的 plans JSON 绝不能写入
    生产 plans/ 目录，避免 CI 脏数据干扰后续真实运行。
    """
    plans_dir = tmp_path / "plans"
    monkeypatch.setattr(storage, "_PLANS_DIR", str(plans_dir))
    yield


# ---------------------------------------------------------------------------
# 1. run_scan：screener→plan.generate→storage.save_plans 编排
# ---------------------------------------------------------------------------
class TestRunScan:
    """run_scan(req: ScanRequest) → list[CandidatePlan]：扫描→生成→落盘→返回。"""

    def test_run_scan_persists_and_returns_plans(self):
        """合成 W 底 price_data → run_scan 落 storage + 返回 CandidatePlan 列表。

        核心断言：
            - 返回值非空（合成标准 W 底必命中候选）；
            - 返回值类型为 list[CandidatePlan]；
            - 计划已落盘（storage.load_plans() 可读回同 plan_id）；
            - CandidatePlan 字段对齐 TradePlan（symbol/pattern_type/entry/stop/...）。

        price_data 语义：模拟生产环境 data_lake 在 T 日提供 df.loc[:T] 的行为——
        末根恰好落在形态确认点（T=19，confirm_bars=2 后 W 底突破被确认），
        使 run_scan 单次调用即命中候选（避免因果 ZigZag 末段 pivot 被丢弃导致漏检）。
        """
        df = _build_detections_w_bottom()
        price_data = {"TESTW.SZ": df}
        req = ScanRequest(
            date="2024-01-15",
            universe=["TESTW.SZ"],
            cfg_override=dict(_LOOSE_CFG_OVERRIDE),
        )

        # 注入合成 price_data（生产由 data_lake 装配，测试直接覆盖）
        import server.services.caisen_service as svc
        original_load = svc._load_price_data
        svc._load_price_data = lambda symbols, date: price_data
        try:
            plans = caisen_service.run_scan(req)
        finally:
            svc._load_price_data = original_load

        # —— 断言：返回候选非空 + 类型正确 ——
        assert len(plans) >= 1, "合成标准 W 底应命中候选"
        assert all(isinstance(p, CandidatePlan) for p in plans)

        p = plans[0]
        # CandidatePlan 字段对齐 TradePlan（关键字段非空 + 类型合法）
        assert p.plan_id  # 非空字符串
        assert p.symbol == "TESTW.SZ"
        assert p.pattern_type in {"w_bottom", "head_shoulder"}
        assert isinstance(p.breakout_price, float)
        assert isinstance(p.entry_upper, float)
        assert isinstance(p.stop_loss, float)
        assert isinstance(p.shares, int)
        assert p.shares >= 0
        assert p.shares % 100 == 0   # A 股整手

        # —— 断言：计划已落盘（storage.load_plans 读回同 plan_id）——
        loaded = storage.load_plans()
        ids_in_storage = {d["plan_id"] for d in loaded}
        returned_ids = {pp.plan_id for pp in plans}
        assert returned_ids.issubset(ids_in_storage), "run_scan 返回的计划必须已落盘"

    def test_run_scan_empty_universe_returns_empty(self):
        """universe 为空 → screener 无输入 → 返回空列表（不抛异常）。"""
        req = ScanRequest(date="2024-01-15", universe=[], cfg_override={})
        plans = caisen_service.run_scan(req)
        assert plans == []

    def test_run_scan_cfg_override_applied(self):
        """cfg_override 增量覆盖默认配置（min_rr_ratio 提至 99 过滤全部）。

        构造：合成 W 底 + 完整宽松 cfg_override + min_rr_ratio=99（极端严格覆盖）
        → 所有计划被盈亏比过滤 → 返回空。验证 cfg_override 真正生效
        （否则完整宽松配置会命中候选）。
        """
        df = _build_detections_w_bottom()
        price_data = {"TESTW.SZ": df}
        override = dict(_LOOSE_CFG_OVERRIDE)
        override["min_rr_ratio"] = 99.0   # 极端严格，过滤全部
        req = ScanRequest(
            date="2024-01-15",
            universe=["TESTW.SZ"],
            cfg_override=override,
        )
        import server.services.caisen_service as svc
        original_load = svc._load_price_data
        svc._load_price_data = lambda symbols, date: price_data
        try:
            plans = caisen_service.run_scan(req)
        finally:
            svc._load_price_data = original_load
        assert plans == [], "min_rr_ratio=99 应过滤掉所有计划（cfg_override 生效）"

    def test_run_scan_validation_error_propagates(self):
        """cfg_override 非法字段 → ValidationError 上抛（非降级空列表）。

        物理意图（Task 3 review I-1）：
            service 层 try/except 不能一锅端吞掉参数错误。cfg_override 含未知字段名
            （如拼写错误 "min_rr_ration"）或非法值（类型/约束违反）时，必须抛
            ValidationError 透传路由层转 422——让前端能区分"参数错误"vs"无候选"。

        校准要点：
            Pydantic v2 的 model_copy(update=...) 是【不触发校验】的浅拷贝，会静默
            接受未知字段；故 service._merge_cfg 改走 model_validate 全字段校验，
            未知键/类型错误统一抛 ValidationError。本测试验证此契约不退化。
        """
        # 拼写错误的字段名（合法字段是 min_rr_ratio）→ _merge_cfg 抛 ValidationError
        bad_override = dict(_LOOSE_CFG_OVERRIDE)
        bad_override["min_rr_ration_typo"] = 1.5   # 未知字段
        req = ScanRequest(
            date="2024-01-15",
            universe=["TESTW.SZ"],
            cfg_override=bad_override,
        )
        with pytest.raises(ValidationError):
            caisen_service.run_scan(req)

    def test_run_scan_value_error_propagates(self):
        """业务参数非法（约束违反）→ ValidationError 上抛（非降级空列表）。

        物理意图：cfg_override 字段名合法但值违反 Pydantic 约束（如 min_pattern_bars
        的 ge=11，传 5 违反下界）→ 同样应抛 ValidationError 透传路由层转 422。
        """
        bad_override = dict(_LOOSE_CFG_OVERRIDE)
        bad_override["min_pattern_bars"] = 5   # 违反 ge=11 约束
        req = ScanRequest(
            date="2024-01-15",
            universe=["TESTW.SZ"],
            cfg_override=bad_override,
        )
        with pytest.raises(ValidationError):
            caisen_service.run_scan(req)


# ---------------------------------------------------------------------------
# 2. list_plans：跨日期合并 + status 过滤
# ---------------------------------------------------------------------------
class TestListPlans:
    """list_plans(status) → list[CandidatePlan]：读盘 + 可选 status 过滤。"""

    def test_list_plans_filter_by_status(self):
        """落两个 status 不同的计划 → list_plans(status=) 精确过滤。"""
        # 直接用 storage 落两个合成计划，分别置 APPROVED / ARMED
        from caisen.plan import TradePlan
        from dataclasses import replace

        base = TradePlan(
            plan_id="list-1", symbol="L1.SZ", pattern_type="w_bottom",
            formed_at=pd.Timestamp("2024-06-01"), breakout_price=10.0,
            neckline_price=11.0, bottom_price=9.0, H=2.0,
            entry_upper=10.0, entry_lower=9.7, stop_loss=9.0,
            take_profit=13.0, take_profit_2x=15.0, rr_ratio=3.0,
            valid_until=pd.Timestamp("2024-06-05"),
            max_holding_until=pd.Timestamp("2024-06-24"),
            timeout_exit_threshold=0.01, shares=1000,
        )
        p2 = replace(base, plan_id="list-2", symbol="L2.SZ")
        storage.save_plans("2024-06-01", [base, p2])
        storage.update_plan("list-1", status="APPROVED")
        storage.update_plan("list-2", status="ARMED")

        # status 过滤
        approved = caisen_service.list_plans(status="APPROVED")
        assert len(approved) == 1
        assert approved[0].plan_id == "list-1"

        armed = caisen_service.list_plans(status="ARMED")
        assert len(armed) == 1
        assert armed[0].plan_id == "list-2"

    def test_list_plans_no_status_returns_all(self):
        """status=None 返回全部计划。"""
        from caisen.plan import TradePlan

        p = TradePlan(
            plan_id="all-1", symbol="A1.SZ", pattern_type="w_bottom",
            formed_at=pd.Timestamp("2024-06-01"), breakout_price=10.0,
            neckline_price=11.0, bottom_price=9.0, H=2.0,
            entry_upper=10.0, entry_lower=9.7, stop_loss=9.0,
            take_profit=13.0, take_profit_2x=15.0, rr_ratio=3.0,
            valid_until=pd.Timestamp("2024-06-05"),
            max_holding_until=pd.Timestamp("2024-06-24"),
            timeout_exit_threshold=0.01, shares=1000,
        )
        storage.save_plans("2024-06-01", [p])
        all_plans = caisen_service.list_plans()
        assert len(all_plans) == 1
        assert all_plans[0].plan_id == "all-1"

    def test_list_plans_empty_when_no_files(self):
        """无 plans 文件 → 返回空列表（不抛异常）。"""
        assert caisen_service.list_plans() == []


# ---------------------------------------------------------------------------
# 3. approve_plan：PENDING_APPROVAL → APPROVED 状态迁移
# ---------------------------------------------------------------------------
class TestApprovePlan:
    """approve_plan(plan_id, review: PlanReview) → CandidatePlan：审核 + 微调。"""

    def test_approve_plan_status_transition(self):
        """approve action → status 推进到 APPROVED。"""
        from caisen.plan import TradePlan

        p = TradePlan(
            plan_id="appr-1", symbol="AP1.SZ", pattern_type="w_bottom",
            formed_at=pd.Timestamp("2024-06-01"), breakout_price=10.0,
            neckline_price=11.0, bottom_price=9.0, H=2.0,
            entry_upper=10.0, entry_lower=9.7, stop_loss=9.0,
            take_profit=13.0, take_profit_2x=15.0, rr_ratio=3.0,
            valid_until=pd.Timestamp("2024-06-05"),
            max_holding_until=pd.Timestamp("2024-06-24"),
            timeout_exit_threshold=0.01, shares=1000,
        )
        storage.save_plans("2024-06-01", [p])
        # 初始 status = PENDING_APPROVAL（save_plans 默认）
        assert storage.get_plan("appr-1")["status"] == "PENDING_APPROVAL"

        review = PlanReview(action="approve", edits={})
        result = caisen_service.approve_plan("appr-1", review)

        assert isinstance(result, CandidatePlan)
        assert result.plan_id == "appr-1"
        assert result.status == "APPROVED"
        # 落盘校验
        assert storage.get_plan("appr-1")["status"] == "APPROVED"

    def test_approve_plan_with_edits_applied(self):
        """approve + edits 微调字段（如人工调整 stop_loss）。

        实盘场景：人工审核时微调止损位/止盈位（基于经验判断的参数微调）。
        """
        from caisen.plan import TradePlan

        p = TradePlan(
            plan_id="appr-2", symbol="AP2.SZ", pattern_type="w_bottom",
            formed_at=pd.Timestamp("2024-06-01"), breakout_price=10.0,
            neckline_price=11.0, bottom_price=9.0, H=2.0,
            entry_upper=10.0, entry_lower=9.7, stop_loss=9.0,
            take_profit=13.0, take_profit_2x=15.0, rr_ratio=3.0,
            valid_until=pd.Timestamp("2024-06-05"),
            max_holding_until=pd.Timestamp("2024-06-24"),
            timeout_exit_threshold=0.01, shares=1000,
        )
        storage.save_plans("2024-06-01", [p])

        review = PlanReview(action="approve", edits={"stop_loss": 8.8, "take_profit": 13.5})
        result = caisen_service.approve_plan("appr-2", review)
        assert result.stop_loss == pytest.approx(8.8)
        assert result.take_profit == pytest.approx(13.5)
        assert result.status == "APPROVED"

    def test_approve_plan_reject_sets_status(self):
        """reject action → status 推进到 REJECTED（不再进入 ARMED/FILLED 流程）。"""
        from caisen.plan import TradePlan

        p = TradePlan(
            plan_id="appr-3", symbol="AP3.SZ", pattern_type="w_bottom",
            formed_at=pd.Timestamp("2024-06-01"), breakout_price=10.0,
            neckline_price=11.0, bottom_price=9.0, H=2.0,
            entry_upper=10.0, entry_lower=9.7, stop_loss=9.0,
            take_profit=13.0, take_profit_2x=15.0, rr_ratio=3.0,
            valid_until=pd.Timestamp("2024-06-05"),
            max_holding_until=pd.Timestamp("2024-06-24"),
            timeout_exit_threshold=0.01, shares=1000,
        )
        storage.save_plans("2024-06-01", [p])

        review = PlanReview(action="reject", edits={})
        result = caisen_service.approve_plan("appr-3", review)
        assert result.status == "REJECTED"

    def test_approve_nonexistent_raises(self):
        """审核不存在的 plan_id → 抛 KeyError（路由层转 404）。

        防御性：状态机不进 NULL，不允许对空 plan_id 静默成功。
        """
        review = PlanReview(action="approve", edits={})
        with pytest.raises(KeyError):
            caisen_service.approve_plan("nonexistent-id", review)


# ---------------------------------------------------------------------------
# 4. activate_plan：APPROVED → ARMED 状态迁移
# ---------------------------------------------------------------------------
class TestActivatePlan:
    """activate_plan(plan_id) → CandidatePlan：置 ARMED（挂单待执行）。"""

    def test_activate_plan_sets_armed(self):
        """activate → status 推进到 ARMED + 同步进 active.json。"""
        from caisen.plan import TradePlan

        p = TradePlan(
            plan_id="act-1", symbol="AC1.SZ", pattern_type="w_bottom",
            formed_at=pd.Timestamp("2024-06-01"), breakout_price=10.0,
            neckline_price=11.0, bottom_price=9.0, H=2.0,
            entry_upper=10.0, entry_lower=9.7, stop_loss=9.0,
            take_profit=13.0, take_profit_2x=15.0, rr_ratio=3.0,
            valid_until=pd.Timestamp("2024-06-05"),
            max_holding_until=pd.Timestamp("2024-06-24"),
            timeout_exit_threshold=0.01, shares=1000,
        )
        storage.save_plans("2024-06-01", [p])
        storage.update_plan("act-1", status="APPROVED")

        result = caisen_service.activate_plan("act-1")
        assert isinstance(result, CandidatePlan)
        assert result.plan_id == "act-1"
        assert result.status == "ARMED"
        # ARMED → 同步进 active.json（执行器高频读路径）
        active = storage.load_active_plans()
        assert any(d["plan_id"] == "act-1" for d in active)

    def test_activate_nonexistent_raises(self):
        """激活不存在的 plan_id → KeyError。"""
        with pytest.raises(KeyError):
            caisen_service.activate_plan("ghost-id")


# ---------------------------------------------------------------------------
# 5. get_plan：单计划查询
# ---------------------------------------------------------------------------
class TestGetPlan:
    """get_plan(plan_id) → CandidatePlan：单计划精确查询。"""

    def test_get_plan_returns_candidate(self):
        from caisen.plan import TradePlan

        p = TradePlan(
            plan_id="get-1", symbol="G1.SZ", pattern_type="w_bottom",
            formed_at=pd.Timestamp("2024-06-01"), breakout_price=10.0,
            neckline_price=11.0, bottom_price=9.0, H=2.0,
            entry_upper=10.0, entry_lower=9.7, stop_loss=9.0,
            take_profit=13.0, take_profit_2x=15.0, rr_ratio=3.0,
            valid_until=pd.Timestamp("2024-06-05"),
            max_holding_until=pd.Timestamp("2024-06-24"),
            timeout_exit_threshold=0.01, shares=1000,
        )
        storage.save_plans("2024-06-01", [p])

        result = caisen_service.get_plan("get-1")
        assert isinstance(result, CandidatePlan)
        assert result.plan_id == "get-1"
        assert result.symbol == "G1.SZ"

    def test_get_plan_nonexistent_returns_none(self):
        """查询不存在的 plan_id → None（路由层转 404）。"""
        assert caisen_service.get_plan("ghost") is None


# ---------------------------------------------------------------------------
# 6. run_replay：历史回放编排
# ---------------------------------------------------------------------------
class TestRunReplay:
    """run_replay(req: ReplayRequest) → ReplayReportResponse：回放统计。"""

    def test_run_replay_returns_report(self):
        """合成 W 底 + 满足涨幅序列 → run_replay 返回 ReplayReportResponse。

        核心断言：
            - 返回类型 ReplayReportResponse；
            - 字段对齐 backtest_replay.ReplayReport（n_hits/win_rate/avg_rr/...）；
            - n_hits ≥ 0（不抛异常，统计降级安全）。
        """
        df = _build_standard_w_bottom_price_df()
        price_data = {"TESTW.SZ": df}
        # 回放区间覆盖合成序列范围（RangeIndex 整数 index）
        n = len(df)
        req = ReplayRequest(
            start=str(n // 2),       # 序列中段开始（保证有足够前置形态形成）
            end=str(n - 1),          # 末根结束
            cfg_override=dict(_LOOSE_CFG_OVERRIDE),
        )

        import server.services.caisen_service as svc
        original_load = svc._load_price_data
        svc._load_price_data = lambda symbols, date: price_data
        try:
            report = caisen_service.run_replay(req)
        finally:
            svc._load_price_data = original_load

        assert isinstance(report, ReplayReportResponse)
        # 字段对齐 ReplayReport
        assert isinstance(report.n_hits, int)
        assert report.n_hits >= 0
        assert isinstance(report.win_rate, float)
        assert isinstance(report.avg_rr, float)
        assert isinstance(report.max_drawdown, float)
        assert isinstance(report.pattern_dist, dict)
        assert isinstance(report.monthly_returns, dict)
        assert isinstance(report.avg_holding_bars, float)
        assert isinstance(report.min_rr_ratio_recommendation, str)
        # win_rate ∈ [0, 1]
        assert 0.0 <= report.win_rate <= 1.0

    def test_run_replay_empty_data_returns_zero_stats(self):
        """无候选命中（空 price_data）→ 返回零统计报告（不抛异常）。"""
        req = ReplayRequest(start="2024-01-01", end="2024-01-31", cfg_override={})
        import server.services.caisen_service as svc
        original_load = svc._load_price_data
        svc._load_price_data = lambda symbols, date: {}
        try:
            report = caisen_service.run_replay(req)
        finally:
            svc._load_price_data = original_load

        assert isinstance(report, ReplayReportResponse)
        assert report.n_hits == 0
        assert report.win_rate == pytest.approx(0.0)

    def test_run_replay_accepts_universe(self):
        """ReplayRequest(universe=[...]) 能传入并下传 _load_price_data（Task 3 review I-2）。

        物理意图：
            run_replay 之前用 universe=None 占位（生产永远降级零统计）。I-2 给
            ReplayRequest 加 universe 字段后，契约层入口就位——即使当前 _load_price_data
            仍占位返空，调用方传 universe=[...] 也应能成功构造 ReplayRequest 并被
            run_replay 接收（不抛异常，下传 symbols 到 _load_price_data）。

        核心断言：
            - ReplayRequest(universe=[...]) 构造成功（schema 接受 universe 字段）；
            - run_replay 接收后不抛异常（返回合法 ReplayReportResponse）；
            - _load_price_data 收到的 symbols == 请求的 universe（捕获 monkeypatch 入参）。
        """
        captured = {}

        def fake_load(symbols, date):
            captured["symbols"] = symbols
            captured["date"] = date
            return {}   # 占位返空 → run_replay 降级零统计（验证契约入口，非回放逻辑）

        req = ReplayRequest(
            start="2024-01-01",
            end="2024-01-31",
            universe=["UNI1.SZ", "UNI2.SZ"],
            cfg_override={},
        )
        # schema 层：universe 字段已就位
        assert req.universe == ["UNI1.SZ", "UNI2.SZ"]

        import server.services.caisen_service as svc
        original_load = svc._load_price_data
        svc._load_price_data = fake_load
        try:
            report = caisen_service.run_replay(req)
        finally:
            svc._load_price_data = original_load

        # service 层：universe 下传到 _load_price_data
        assert captured.get("symbols") == ["UNI1.SZ", "UNI2.SZ"]
        # run_replay 不抛异常，降级零统计
        assert isinstance(report, ReplayReportResponse)

    def test_run_replay_universe_none_defaults_to_empty(self):
        """ReplayRequest(universe=None) → 全市场占位（_load_price_data 收到 []）。"""
        captured = {}

        def fake_load(symbols, date):
            captured["symbols"] = symbols
            return {}

        req = ReplayRequest(
            start="2024-01-01", end="2024-01-31", cfg_override={},  # universe 默认 None
        )
        assert req.universe is None   # 默认值 = None = 全市场

        import server.services.caisen_service as svc
        original_load = svc._load_price_data
        svc._load_price_data = fake_load
        try:
            caisen_service.run_replay(req)
        finally:
            svc._load_price_data = original_load

        # None → service 内部归一化为 []（_load_price_data 占位全市场语义）
        assert captured.get("symbols") == []
