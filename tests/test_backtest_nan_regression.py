"""回归测试：SSE result 帧必须是无 NaN 的合法 JSON（否则前端 K 线不显示）。

背景 bug（2026-07 排查）：
  backtest/engine.py 的 `daily_df["return"].iloc[0] = 0.0` 是 chained assignment，
  pandas Copy-on-Write 下作用于临时副本、原 df 不变 → 首行 return 残留 NaN。
  backtest_service._serialize_backtest_result 的 `where(np.isfinite, None)` 因
  pandas float 列 None→NaN 坑无效，NaN 流入 NavPoint（return_ 字段）。
  SSE 路径 `json.dumps(allow_nan=True 默认)` 把 NaN 输出为字面 NaN（非法 JSON），
  浏览器 JSON.parse 失败，前端 useTerminalState 的 catch 静默丢弃 result 帧，
  state.result 永远 null → ProChart v-if=false → K 线/买卖点不显示。

这两个测试守住「整条 nav_series 有限」与「result 帧可被浏览器严格解析」两条硬契约。
"""
import json
import math
from datetime import date

import pytest
from fastapi.encoders import jsonable_encoder

from server.schemas.backtest import BacktestRequest
from server.services.backtest_service import run_single_backtest


@pytest.fixture(scope="module")
def backtest_resp():
    """跑一次单资产回测（module 级共享，省去重复计算开销）。"""
    return run_single_backtest(BacktestRequest(
        symbol="dynamic_top50",
        start_date=date(2023, 1, 1),
        end_date=date(2024, 12, 31),
        initial_capital=1_000_000,
        signal_freq="1d",
        strategy_name="tech_macro_fusion",
        strategy_params={},
    ))


def test_nav_series_finite(backtest_resp):
    """nav_series 每个节点的 nav/return/cumulative_return 必须是有限数。

    回归 engine.py:1224 chained-assignment 失效致首行 return=NaN 的 bug。
    """
    assert len(backtest_resp.nav_series) > 0, "nav_series 不应为空"
    for i, p in enumerate(backtest_resp.nav_series):
        assert math.isfinite(p.nav), f"nav_series[{i}].nav 非有限: {p.nav}"
        # 首行 return 是 chained-assignment 回归点，重点断言
        assert math.isfinite(p.return_), (
            f"nav_series[{i}].return 非有限: {p.return_}（疑似 chained-assignment 回归）"
        )
        assert math.isfinite(p.cumulative_return), (
            f"nav_series[{i}].cumulative_return 非有限: {p.cumulative_return}"
        )


def test_sse_result_frame_valid_json(backtest_resp):
    """模拟 SSE result 帧序列化：allow_nan=False 必须不抛且不含 NaN/Infinity 字面。

    浏览器 JSON.parse 等价 allow_nan=False 严格模式，出现 NaN/Infinity 必抛。
    这是前端能成功解析 result 帧、渲染 K 线的硬条件。
    """
    payload = jsonable_encoder({"type": "result", "data": backtest_resp})
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in serialized, "result 帧含字面 NaN，前端 JSON.parse 必失败"
    assert "Infinity" not in serialized, "result 帧含字面 Infinity，前端 JSON.parse 必失败"


def test_positions_snapshot_reflects_real_holding(backtest_resp):
    """末态持仓快照应真实反映 run_portfolio 的 positions dict（qty>0）+ 详情字段。

    回归：_extract_positions 曾按单资产 run 的 position 标量列取值，但单资产回测走
    run_portfolio，daily_records 是组合结构（positions dict / position_value 标量），
    没有 position 列 → qty 恒取 0；mv 误取总市值标量。表现为「0 股 + 15 万市值」错乱。
    详情字段（avg_cost/open_date/cash/nav）由 _compute_cost_basis + daily_records 算得。
    """
    assert len(backtest_resp.positions) > 0, "末态有持仓（470/522 日），positions 不应为空"
    p = backtest_resp.positions[0]
    # 基础字段：从 positions/position_values dict 取
    assert p.qty > 0, (
        f"持仓数量应 >0（真实 1500），实际 {p.qty}（_extract_positions 与 run_portfolio 结构不匹配）"
    )
    assert p.market_value > 0, f"持仓市值应 >0，实际 {p.market_value}"
    # 详情字段：从 trades 加权平均 + daily_records 末行
    assert p.avg_cost > 0, f"持仓成本应 >0，实际 {p.avg_cost}"
    assert p.open_date is not None, "建仓日期不应为 None（trades 有 buy 记录）"
    assert p.holding_days >= 0
    assert p.cash > 0, f"末态现金应 >0，实际 {p.cash}"
    assert p.nav > 0, f"末态总资产应 >0，实际 {p.nav}"
    # 对账：cash + 持仓市值 ≈ nav（AUM = cash + positions_value，容许 1 元浮点误差）
    assert abs((p.cash + p.market_value) - p.nav) < 1.0, (
        f"cash+mv 与 nav 应对账：{p.cash + p.market_value} vs {p.nav}"
    )
