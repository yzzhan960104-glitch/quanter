# -*- coding: utf-8 -*-
"""蔡森形态学流水线 REST 端点契约测试（Phase 3 · Task 4）。

物理意图与覆盖节点（CLAUDE.md 量化风控·边界审查）：
    本测试验证 server/api/v1/caisen.py 的 7 个 REST 端点——把 caisen_service 的
    六个编排函数（run_scan / list_plans / approve_plan / activate_plan / get_plan /
    run_replay）+ chart/positions 占位端点封装为 HTTP 友好接口，并对 service 层透传
    的 KeyError / ValidationError / ValueError 做正确的状态码转译。

    覆盖节点：
      1. test_scan_returns_plans：POST /caisen/scan 200 + plans 列表（合成 W 底命中）；
      2. test_list_plans_filter：GET /caisen/plans?status=APPROVED 200 + 精确过滤；
      3. test_get_plan_404：GET /caisen/plans/nonexistent 404（KeyError 转译）；
      4. test_get_plan_returns_candidate：GET /caisen/plans/{id} 200 + CandidatePlan 字段；
      5. test_patch_plan_approve：PATCH /caisen/plans/{id} approve → 200 + APPROVED 状态迁移；
      6. test_activate_plan：POST /caisen/plans/{id}/activate → 200 + ARMED；
      7. test_replay：POST /caisen/replay → 200 + ReplayReportResponse；
      8. test_scan_validation_error_422：cfg_override 非法字段 → 422（ValidationError 转译）；
      9. test_get_chart_returns_plan_info：GET /caisen/plans/{id}/chart 200（占位）；
     10. test_get_positions_returns_200：GET /caisen/positions 200（占位空结构）。

设计要点（CLAUDE.md 极简 + 显式原则）：
    - 全程用 tmp_path fixture 隔离 storage（monkeypatch storage._PLANS_DIR），绝不污染真实 plans/；
    - 合成 price_data 复用 Task 3 service 测试已验证的标准 W 底序列（同源 _LOOSE_CFG_OVERRIDE），
      保证 screener.screen 能产出非空候选，plan.generate 能产出 rr≥min_rr 的计划；
    - caisen_service._load_price_data 通过 monkeypatch 注入合成数据，模拟生产 data_lake 装配。

蔡森方法学对齐：
    server 层 REST 路由是蔡森流水线的"对外 HTTP 契约层"——前端/调度器只感知这一层。
    路由零业务逻辑（仅异常转译 + 透传 service），所有数学内核在 Phase 2 完成。
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from caisen import storage
from caisen.plan import TradePlan


# ---------------------------------------------------------------------------
# 合成 W 底序列构造（与 test_caisen_service.py 同源，保证 screener 能命中）
# ---------------------------------------------------------------------------
# 宽松 cfg_override：生产默认 StrategyConfig() 严格（confirm_bars=3/ma26w_filter=True/
# abc_wave_detect=True 等），合成标准 W 底在严格默认下会被否决。测试需传完整宽松
# override 才能复现 Task 8/10 已验证的命中场景。min_rr_ratio=1.0 承 rr 张力
# （Bug4 修复后新 rr 公式下标准 W 底 rr≈1.4，生产默认 3.0 会过滤掉所有样本；
# min_rr_ratio 定标是独立 Phase3+ 待办）。
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


def _w_vol_pattern(n: int, p1_i: int, p3_i: int, p4_i: int) -> pd.Series:
    """W 底量价模式（同 Task 8/10）：左底放量 + 右底缩量 + 突破放量。"""
    vol = pd.Series(200.0, index=pd.RangeIndex(n))
    vol.iloc[p1_i] = 300.0   # 左底放量
    vol.iloc[p3_i] = 100.0   # 右底缩量
    vol.iloc[p4_i] = 500.0   # 突破日放量
    return vol


def _build_standard_w_bottom_price_df() -> pd.DataFrame:
    """合成标准 W 底 + 满足涨幅段的 OHLCV DataFrame（复用 Task 8/10 已验证序列）。"""
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
    high = close + 0.3
    low = close - 0.3
    vol = _w_vol_pattern(n, p1_i=5, p3_i=13, p4_i=17)
    tail_vol = pd.Series(250.0, index=pd.RangeIndex(len(pre_close), n))
    vol.iloc[len(pre_close):] = tail_vol.values
    amount = pd.Series(2e8, index=pd.RangeIndex(n), dtype=float)

    return pd.DataFrame({
        "close": close.values,
        "high": high.values,
        "low": low.values,
        "volume": vol.values,
        "amount": amount.values,
    }, index=pd.RangeIndex(n))


def _build_detections_w_bottom() -> pd.DataFrame:
    """截取到形态确认点（T=19），保证 run_scan 单次调用即命中。"""
    full = _build_standard_w_bottom_price_df()
    return full.loc[:19].copy()


def _mk_trade_plan(plan_id: str, symbol: str) -> TradePlan:
    """构造一个合法的 TradePlan（用于直接落盘测试 PATCH/activate/get_plan 端点）。"""
    return TradePlan(
        plan_id=plan_id, symbol=symbol, pattern_type="w_bottom",
        formed_at=pd.Timestamp("2024-06-01"), breakout_price=10.0,
        neckline_price=11.0, bottom_price=9.0, H=2.0,
        entry_upper=10.0, entry_lower=9.7, stop_loss=9.0,
        take_profit=13.0, take_profit_2x=15.0, rr_ratio=3.0,
        valid_until=pd.Timestamp("2024-06-05"),
        max_holding_until=pd.Timestamp("2024-06-24"),
        timeout_exit_threshold=0.01, shares=1000,
    )


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    """构造 FastAPI TestClient（复用 server.main:app 单例）。

    Why import 后构造：server.main 模块级 app 已注册全部路由（含 Task 4 挂载的
    caisen_router），TestClient 生命周期内可触发 lifespan（含多湖 load），CI 无数据湖
    时 reader.load 对缺失 parquet 仅记 warning 不阻断（离线降级契约）。
    """
    from server.main import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
    """每个测试自动隔离：storage._PLANS_DIR + replay_runs._REPLAY_RUNS_DIR 指向 tmp_path。

    防御性（CLAUDE.md 量化风控·边界审查）：测试落盘的 plans / replay_runs JSON 绝不能
    写入生产目录。方案 A 让 POST /replay 默认落盘历史——若不隔离 replay_runs，test_replay
    会把测试回放写进真实 replay_runs/ 污染生产，故两目录一并拦在 tmp_path。
    """
    from caisen import replay_runs
    plans_dir = tmp_path / "plans"
    runs_dir = tmp_path / "replay_runs"
    monkeypatch.setattr(storage, "_PLANS_DIR", str(plans_dir))
    monkeypatch.setattr(replay_runs, "_REPLAY_RUNS_DIR", str(runs_dir))
    yield


@pytest.fixture
def inject_price_data(monkeypatch):
    """注入合成 W 底 price_data 到 caisen_service._load_price_data（模拟生产 data_lake）。

    用法：
        plans = inject_price_data({"TESTW.SZ": df})
    返回的 price_data 同时被注入，调用方按需读取。
    """
    import server.services.caisen_service as svc

    def _inject(price_data):
        monkeypatch.setattr(svc, "_load_price_data", lambda symbols, date: price_data)

    return _inject


# ---------------------------------------------------------------------------
# 1. POST /caisen/scan —— 扫描编排（200 + plans 列表）
# ---------------------------------------------------------------------------
def test_scan_returns_plans(client, inject_price_data):
    """合成标准 W 底 → POST /caisen/scan 返回 200 + CandidatePlan 列表。

    核心断言：
        - HTTP 200（非 500：算法/IO 异常在 service 层已降级，路由层不报错）；
        - 返回 list[CandidatePlan]，非空（合成 W 底必命中候选）；
        - CandidatePlan 字段对齐 TradePlan（symbol/pattern_type/entry/stop/shares）。
    """
    df = _build_detections_w_bottom()
    inject_price_data({"TESTW.SZ": df})

    resp = client.post("/api/v1/caisen/scan", json={
        "date": "2024-01-15",
        "universe": ["TESTW.SZ"],
        "cfg_override": dict(_LOOSE_CFG_OVERRIDE),
    })

    assert resp.status_code == 200, resp.text
    plans = resp.json()
    assert isinstance(plans, list)
    assert len(plans) >= 1, "合成标准 W 底应命中候选"
    p = plans[0]
    assert p["symbol"] == "TESTW.SZ"
    assert p["pattern_type"] in {"w_bottom", "head_shoulder"}
    # 状态机初始态 PENDING_APPROVAL（save_plans 默认）
    assert p["status"] == "PENDING_APPROVAL"


# ---------------------------------------------------------------------------
# 2. GET /caisen/plans —— 读盘 + status 过滤
# ---------------------------------------------------------------------------
def test_list_plans_filter(client):
    """落两个 status 不同的计划 → GET /caisen/plans?status=APPROVED 精确过滤。"""
    p1 = _mk_trade_plan("list-1", "L1.SZ")
    p2 = _mk_trade_plan("list-2", "L2.SZ")
    storage.save_plans("2024-06-01", [p1, p2])
    storage.update_plan("list-1", status="APPROVED")
    storage.update_plan("list-2", status="ARMED")

    # status=APPROVED 精确过滤
    resp = client.get("/api/v1/caisen/plans", params={"status": "APPROVED"})
    assert resp.status_code == 200
    plans = resp.json()
    assert isinstance(plans, list)
    assert len(plans) == 1
    assert plans[0]["plan_id"] == "list-1"
    assert plans[0]["status"] == "APPROVED"

    # 无 status 参数 = 全部
    resp_all = client.get("/api/v1/caisen/plans")
    assert resp_all.status_code == 200
    assert len(resp_all.json()) == 2


# ---------------------------------------------------------------------------
# 3. GET /caisen/plans/{plan_id} —— 单计划查询 + 404 转译
# ---------------------------------------------------------------------------
def test_get_plan_404(client):
    """查询不存在的 plan_id → 404（service 返 None → 路由层转 HTTPException 404）。

    防御性：状态机不进 NULL，对空 plan_id 显式 404 而非静默 200 空响应。
    """
    resp = client.get("/api/v1/caisen/plans/nonexistent-id")
    assert resp.status_code == 404


def test_get_plan_returns_candidate(client):
    """查询存在的 plan_id → 200 + CandidatePlan 字段。"""
    p = _mk_trade_plan("get-1", "G1.SZ")
    storage.save_plans("2024-06-01", [p])

    resp = client.get("/api/v1/caisen/plans/get-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_id"] == "get-1"
    assert body["symbol"] == "G1.SZ"
    assert body["pattern_type"] == "w_bottom"


# ---------------------------------------------------------------------------
# 4. PATCH /caisen/plans/{plan_id} —— 审核（approve/reject）+ 微调
# ---------------------------------------------------------------------------
def test_patch_plan_approve(client):
    """PATCH approve → 200 + status 迁移 PENDING_APPROVAL→APPROVED + edits 应用。

    核心断言：
        - HTTP 200（非 422/404）；
        - 返回 CandidatePlan，status=APPROVED；
        - edits 微调生效（stop_loss 被人工调整）。
    """
    p = _mk_trade_plan("appr-1", "AP1.SZ")
    storage.save_plans("2024-06-01", [p])

    resp = client.patch("/api/v1/caisen/plans/appr-1", json={
        "action": "approve",
        "edits": {"stop_loss": 8.8, "take_profit": 13.5},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] == "appr-1"
    assert body["status"] == "APPROVED"
    assert body["stop_loss"] == pytest.approx(8.8)
    assert body["take_profit"] == pytest.approx(13.5)
    # 落盘校验
    assert storage.get_plan("appr-1")["status"] == "APPROVED"


def test_patch_plan_reject(client):
    """PATCH reject → 200 + status=REJECTED。"""
    p = _mk_trade_plan("appr-2", "AP2.SZ")
    storage.save_plans("2024-06-01", [p])

    resp = client.patch("/api/v1/caisen/plans/appr-2", json={"action": "reject"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"


def test_patch_plan_404(client):
    """PATCH 不存在的 plan_id → 404（KeyError 转译）。"""
    resp = client.patch("/api/v1/caisen/plans/ghost", json={"action": "approve"})
    assert resp.status_code == 404


def test_patch_plan_invalid_action_422(client):
    """PATCH action 非法（非 approve/reject）→ 422（ValueError 转译）。"""
    p = _mk_trade_plan("appr-3", "AP3.SZ")
    storage.save_plans("2024-06-01", [p])

    resp = client.patch("/api/v1/caisen/plans/appr-3", json={"action": "bogus"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 5. POST /caisen/plans/{plan_id}/activate —— APPROVED → ARMED
# ---------------------------------------------------------------------------
def test_activate_plan(client):
    """POST activate → 200 + status=ARMED + 同步进 active.json。"""
    p = _mk_trade_plan("act-1", "AC1.SZ")
    storage.save_plans("2024-06-01", [p])
    storage.update_plan("act-1", status="APPROVED")

    resp = client.post("/api/v1/caisen/plans/act-1/activate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] == "act-1"
    assert body["status"] == "ARMED"
    # ARMED → 同步进 active.json（执行器高频读路径）
    active = storage.load_active_plans()
    assert any(d["plan_id"] == "act-1" for d in active)


def test_activate_plan_404(client):
    """激活不存在的 plan_id → 404（KeyError 转译）。"""
    resp = client.post("/api/v1/caisen/plans/ghost/activate")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 6. POST /caisen/replay —— 历史回放
# ---------------------------------------------------------------------------
def test_replay(client, inject_price_data):
    """POST /caisen/replay → 200 + ReplayReportResponse（字段对齐 ReplayReport）。

    核心断言：
        - HTTP 200；
        - 字段对齐 ReplayReport（n_hits/win_rate/avg_rr/max_drawdown/pattern_dist/...）；
        - n_hits ≥ 0（不抛异常，统计降级安全）。
    """
    df = _build_standard_w_bottom_price_df()
    inject_price_data({"TESTW.SZ": df})
    n = len(df)

    resp = client.post("/api/v1/caisen/replay", json={
        "start": str(n // 2),
        "end": str(n - 1),
        "cfg_override": dict(_LOOSE_CFG_OVERRIDE),
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["n_hits"], int)
    assert body["n_hits"] >= 0
    assert isinstance(body["win_rate"], float)
    assert 0.0 <= body["win_rate"] <= 1.0
    assert isinstance(body["avg_rr"], float)
    assert isinstance(body["max_drawdown"], float)
    assert isinstance(body["pattern_dist"], dict)
    assert isinstance(body["monthly_returns"], dict)
    assert isinstance(body["avg_holding_bars"], float)
    assert isinstance(body["min_rr_ratio_recommendation"], str)


def test_replay_empty_data_zero_stats(client, inject_price_data):
    """无 price_data → 200 + 零统计报告（不抛异常，service 降级）。"""
    inject_price_data({})

    resp = client.post("/api/v1/caisen/replay", json={
        "start": "2024-01-01",
        "end": "2024-01-31",
        "cfg_override": {},
    })
    assert resp.status_code == 200
    assert resp.json()["n_hits"] == 0


# ---------------------------------------------------------------------------
# 7. POST /caisen/scan —— ValidationError → 422
# ---------------------------------------------------------------------------
def test_scan_validation_error_422(client, inject_price_data):
    """cfg_override 含非法字段名 → 422（ValidationError 透传路由层转译）。

    物理意图（Task 3 review I-1）：
        service 层 try/except 不能一锅端吞掉参数错误。cfg_override 含未知字段名
        （如拼写错误 "min_rr_ration_typo"）时必须抛 ValidationError 透传路由层转 422——
        让前端能区分"参数错误"vs"无候选"。本测试锁定此转译契约不退化。
    """
    df = _build_detections_w_bottom()
    inject_price_data({"TESTW.SZ": df})

    bad_override = dict(_LOOSE_CFG_OVERRIDE)
    bad_override["min_rr_ration_typo"] = 1.5   # 拼写错误的字段名 → 未知字段

    resp = client.post("/api/v1/caisen/scan", json={
        "date": "2024-01-15",
        "universe": ["TESTW.SZ"],
        "cfg_override": bad_override,
    })
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 8. GET /caisen/plans/{plan_id}/chart —— 占位端点（200 + plan 基本信息）
# ---------------------------------------------------------------------------
def test_get_chart_returns_plan_info(client):
    """GET /caisen/plans/{id}/chart → 200 + plan 基本信息（占位，Task 6 完善 viz）。

    物理意图：本端点当前返回 plan 字段 + 占位 chart 结构，Task 6 接 lightweight-charts
    数据（颈线/止损/止盈标注线 + 末段 K 线）后完善。当前仅锁定"端点可达 + 不抛 404"契约。
    """
    p = _mk_trade_plan("chart-1", "C1.SZ")
    storage.save_plans("2024-06-01", [p])

    resp = client.get("/api/v1/caisen/plans/chart-1/chart")
    assert resp.status_code == 200
    body = resp.json()
    # 至少含 plan_id（确认命中了正确的 plan）
    assert body.get("plan_id") == "chart-1"


def test_get_chart_404(client):
    """chart 端点查不存在 plan → 404（与 get_plan 一致）。"""
    resp = client.get("/api/v1/caisen/plans/ghost/chart")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 9. GET /caisen/positions —— 形态学持仓（占位空结构）
# ---------------------------------------------------------------------------
def test_get_positions_returns_200(client):
    """GET /caisen/positions → 200 + 列表结构（占位，后续接 trading_service 富化）。

    物理意图：本端点当前占位返回空 positions 列表，后续接入 trading_service.get_positions
    做形态学持仓富化（关联 plan_id + 实时盈亏）。当前仅锁定"端点可达 + 返 200 + 列表结构"。
    """
    resp = client.get("/api/v1/caisen/positions")
    assert resp.status_code == 200
    body = resp.json()
    # positions 字段为 list（占位空列表或富化后的持仓记录）
    assert "positions" in body
    assert isinstance(body["positions"], list)


# ---------------------------------------------------------------------------
# 10. 回测历史记录（方案 A：GET /replay/runs · GET /replay/runs/{id} · DELETE）
# ---------------------------------------------------------------------------
def test_replay_response_carries_run_id(client, inject_price_data):
    """POST /replay 默认 save=True → 响应回填 run_id（前端据此显示「已保存」）。"""
    df = _build_standard_w_bottom_price_df()
    inject_price_data({"TESTW.SZ": df})
    n = len(df)

    resp = client.post("/api/v1/caisen/replay", json={
        "start": str(n // 2),
        "end": str(n - 1),
        "cfg_override": dict(_LOOSE_CFG_OVERRIDE),
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["run_id"], "默认 save=True 应落盘并回填 run_id"


def test_replay_save_false_no_run_id(client, inject_price_data):
    """POST /replay save=false → 不落盘，run_id=null（一次性回放）。"""
    df = _build_standard_w_bottom_price_df()
    inject_price_data({"TESTW.SZ": df})
    n = len(df)

    resp = client.post("/api/v1/caisen/replay", json={
        "start": str(n // 2),
        "end": str(n - 1),
        "cfg_override": dict(_LOOSE_CFG_OVERRIDE),
        "save": False,
    })
    assert resp.status_code == 200
    assert resp.json()["run_id"] is None

    # 历史列表仍为空（未落盘）
    assert client.get("/api/v1/caisen/replay/runs").json() == []


def test_list_replay_runs_empty(client):
    """无历史 → GET /caisen/replay/runs 200 + []。"""
    resp = client.get("/api/v1/caisen/replay/runs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_replay_runs_after_replay(client, inject_price_data):
    """跑一次 replay → GET /caisen/replay/runs 含本次摘要（降序，最新在前）。"""
    df = _build_standard_w_bottom_price_df()
    inject_price_data({"TESTW.SZ": df})
    n = len(df)
    # 跑两次（验证多次都存 + 降序）
    r1 = client.post("/api/v1/caisen/replay", json={
        "start": str(n // 2), "end": str(n - 1),
        "cfg_override": dict(_LOOSE_CFG_OVERRIDE),
    }).json()
    r2 = client.post("/api/v1/caisen/replay", json={
        "start": str(n // 2), "end": str(n - 1),
        "cfg_override": dict(_LOOSE_CFG_OVERRIDE),
    }).json()
    assert r1["run_id"] != r2["run_id"], "方案 A：重复回放产生不同 run_id（不去重）"

    resp = client.get("/api/v1/caisen/replay/runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) == 2
    # 降序：最新（r2）在前
    assert runs[0]["run_id"] == r2["run_id"]
    assert runs[1]["run_id"] == r1["run_id"]
    # 摘要关键字段
    for k in ("run_id", "created_at", "start", "end", "universe_n",
              "n_hits", "win_rate", "avg_rr", "annualized_return", "max_drawdown"):
        assert k in runs[0]
    # 摘要不含完整 trades（list 轻量契约）
    assert "trades" not in runs[0]
    assert "equity_curve" not in runs[0]


def test_get_replay_run_returns_detail(client, inject_price_data):
    """GET /caisen/replay/runs/{id} → 200 + ReplayRunDetail（summary + report + request）。"""
    df = _build_standard_w_bottom_price_df()
    inject_price_data({"TESTW.SZ": df})
    n = len(df)
    run_id = client.post("/api/v1/caisen/replay", json={
        "start": str(n // 2), "end": str(n - 1),
        "universe": ["TESTW.SZ"],
        "cfg_override": dict(_LOOSE_CFG_OVERRIDE),
    }).json()["run_id"]

    resp = client.get(f"/api/v1/caisen/replay/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"]["run_id"] == run_id
    assert isinstance(body["report"]["n_hits"], int)
    # request 完整保留（前端可回填表单重跑）
    assert body["request"]["start"] == str(n // 2)
    assert body["request"]["universe"] == ["TESTW.SZ"]


def test_get_replay_run_404(client):
    """GET 不存在/非法 run_id → 404（service 返 None → 路由转 HTTPException）。"""
    resp = client.get("/api/v1/caisen/replay/runs/20240101-000000-deadbe")
    assert resp.status_code == 404
    # 非法格式（路径遍历防御同源）→ 404
    assert client.get("/api/v1/caisen/replay/runs/garbage").status_code == 404


def test_delete_replay_run(client, inject_price_data):
    """DELETE /caisen/replay/runs/{id} → 200 {ok:true}；再 GET/DELETE → 404。"""
    df = _build_standard_w_bottom_price_df()
    inject_price_data({"TESTW.SZ": df})
    n = len(df)
    run_id = client.post("/api/v1/caisen/replay", json={
        "start": str(n // 2), "end": str(n - 1),
        "cfg_override": dict(_LOOSE_CFG_OVERRIDE),
    }).json()["run_id"]

    resp = client.delete(f"/api/v1/caisen/replay/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    # 文件已删 → 再 GET/DELETE 都 404
    assert client.get(f"/api/v1/caisen/replay/runs/{run_id}").status_code == 404
    assert client.delete(f"/api/v1/caisen/replay/runs/{run_id}").status_code == 404
    # 列表也同步清空
    assert client.get("/api/v1/caisen/replay/runs").json() == []


def test_delete_replay_run_404(client):
    """DELETE 不存在 run_id → 404。"""
    resp = client.delete("/api/v1/caisen/replay/runs/20240101-000000-deadbe")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 异步回测端点（Spec 1 · Task 6：POST /replay/async + GET /replay/tasks + cancel）
# 与老同步 /replay 并存：异步任务走 SQLite 任务表，全生命周期可观测/可取消。
# ---------------------------------------------------------------------------
class TestReplayAsyncEndpoints:
    """异步回测 4 端点契约：提交→PENDING、列表、详情（含 report）、取消。"""

    def _isolate_db(self, tmp_path, monkeypatch):
        """隔离 replay_tasks_db 到 tmp_path（不污染生产 data/replay_tasks.db）。"""
        from caisen import replay_tasks_db
        monkeypatch.setattr(replay_tasks_db, "_DEFAULT_DB_PATH", str(tmp_path / "t.db"))
        replay_tasks_db.init_db()

    def test_post_replay_async_returns_task_id(self, client, tmp_path, monkeypatch):
        """POST /replay/async → 200 + {task_id}，任务表写入 PENDING 行。"""
        self._isolate_db(tmp_path, monkeypatch)
        from caisen import replay_tasks_db

        resp = client.post("/api/v1/caisen/replay/async", json={
            "start": "2024-01-01", "end": "2024-06-01",
            "universe": ["000001.SZ"], "cfg_override": {"min_rr_ratio": 1.5},
        })
        assert resp.status_code == 200, resp.text
        tid = resp.json()["task_id"]
        got = replay_tasks_db.get_task(tid)
        assert got["status"] == "PENDING"
        assert got["universe"] == ["000001.SZ"]

    def test_list_replay_tasks(self, client, tmp_path, monkeypatch):
        """GET /replay/tasks → 200 + 任务列表（PENDING 行可见）。"""
        self._isolate_db(tmp_path, monkeypatch)
        from caisen import replay_tasks_db
        replay_tasks_db.create_task(
            {"start": "s", "end": "e", "universe": None, "cfg_override": {}})

        resp = client.get("/api/v1/caisen/replay/tasks")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["status"] == "PENDING"

    def test_get_replay_task_404(self, client, tmp_path, monkeypatch):
        """GET /replay/tasks/{不存在} → 404。"""
        self._isolate_db(tmp_path, monkeypatch)
        resp = client.get("/api/v1/caisen/replay/tasks/nope-id")
        assert resp.status_code == 404

    def test_get_replay_task_detail_with_report(self, client, tmp_path, monkeypatch):
        """GET /replay/tasks/{id} → 200 + 详情（SUCCESS 行内嵌 report）。"""
        self._isolate_db(tmp_path, monkeypatch)
        from caisen import replay_tasks_db
        tid = replay_tasks_db.create_task(
            {"start": "s", "end": "e", "universe": None, "cfg_override": {}})
        replay_tasks_db.mark_success(tid, '{"n_hits": 7}')

        resp = client.get(f"/api/v1/caisen/replay/tasks/{tid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["progress"] == 100
        assert body["report"] == {"n_hits": 7}

    def test_cancel_returns_503_when_scheduler_not_attached(self, client, tmp_path, monkeypatch):
        """调度器未装配（app.state.replay_scheduler 缺，Task 7 前）→ cancel 返 503。"""
        self._isolate_db(tmp_path, monkeypatch)
        # 兜底：若前序用例残留，先摘掉（测试结束 monkeypatch 不恢复手动 setattr，故显式清）
        if hasattr(client.app.state, "replay_scheduler"):
            monkeypatch.delattr(client.app.state, "replay_scheduler", raising=False)
        resp = client.post("/api/v1/caisen/replay/tasks/some-id/cancel")
        assert resp.status_code == 503

    def test_cancel_sets_abort_flag_when_scheduler_attached(self, client, tmp_path, monkeypatch):
        """调度器已装配 → cancel 调 request_cancel → 200 + cancelled=True + abort_flag 已 set。"""
        import multiprocessing as mp
        self._isolate_db(tmp_path, monkeypatch)
        from caisen import replay_tasks_db
        tid = replay_tasks_db.create_task(
            {"start": "s", "end": "e", "universe": None, "cfg_override": {}})
        flag = mp.Event()

        class _FakeSched:
            def request_cancel(self, task_id):
                flag.set()

        client.app.state.replay_scheduler = _FakeSched()
        try:
            resp = client.post(f"/api/v1/caisen/replay/tasks/{tid}/cancel")
            assert resp.status_code == 200
            assert resp.json()["cancelled"] is True
            assert flag.is_set()
        finally:
            # 清理：避免污染后续用例的 app.state（cancel 503 用例依赖未装配）
            if hasattr(client.app.state, "replay_scheduler"):
                del client.app.state.replay_scheduler

    def test_delete_replay_task_200_and_404(self, client, tmp_path, monkeypatch):
        """DELETE /replay/tasks/{id} → 200 {ok:true}；不存在 → 404。

        物理意图：任务历史清理能力（spec §5 交互6）。后端薄路由不加状态守卫——
        「不删 RUNNING」是前端 UX 约定（RUNNING 行无删除按钮），非后端硬约束。
        """
        self._isolate_db(tmp_path, monkeypatch)
        from caisen import replay_tasks_db
        tid = replay_tasks_db.create_task(
            {"start": "s", "end": "e", "universe": None, "cfg_override": {}})

        # 存在 → 200 + {ok:true}，且 DB 行确实删除
        resp = client.delete(f"/api/v1/caisen/replay/tasks/{tid}")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        assert replay_tasks_db.get_task(tid) is None

        # 不存在 → 404（与 get 同源契约，状态机不进 NULL）
        resp2 = client.delete("/api/v1/caisen/replay/tasks/nope-id")
        assert resp2.status_code == 404
