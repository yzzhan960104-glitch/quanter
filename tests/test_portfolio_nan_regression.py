"""组合回测 NaN 回归测试（与单资产 test_backtest_nan_regression 对称）。

背景：_serialize_portfolio_result 原与单资产同款隐患——NavPoint/DrawdownPoint/
TradeRecord 未过 _safe_float，且 where(np.isfinite, None) 对 pandas float 列无效
（None→NaN），首行 return=NaN 致 SSE/同步响应含字面 NaN。已对称修复（标量出口
全过 _safe_float），本测试守住组合路径不再回退。
"""
import json
import math
from datetime import date

import pytest
from fastapi.encoders import jsonable_encoder

from server.schemas.portfolio import PortfolioRequest
from server.services.portfolio_service import run_portfolio_backtest


@pytest.fixture(scope="module")
def portfolio_resp():
    """跑一次组合回测（module 级共享，HMM 训练较重，省去重复计算）。"""
    return run_portfolio_backtest(PortfolioRequest(
        symbols=["510300.SH", "511010.SH"],
        start_date=date(2023, 1, 1),
        end_date=date(2024, 12, 31),
        initial_capital=1_000_000,
        n_hmm_states=3,
        buffer_threshold=0.05,
        state_weights={
            "State_0": {"510300.SH": 0.8, "511010.SH": 0.2},
            "State_1": {"510300.SH": 0.2, "511010.SH": 0.8},
            "State_2": {"510300.SH": 0.5, "511010.SH": 0.5},
        },
        strategy_params={},
    ))


def test_portfolio_nav_series_finite(portfolio_resp):
    """nav_series 每个节点的 nav/return/cumulative_return 必须有限（组合路径回归）。"""
    assert len(portfolio_resp.nav_series) > 0, "nav_series 不应为空"
    for i, p in enumerate(portfolio_resp.nav_series):
        assert math.isfinite(p.nav), f"nav_series[{i}].nav 非有限: {p.nav}"
        assert math.isfinite(p.return_), f"nav_series[{i}].return 非有限: {p.return_}"
        assert math.isfinite(p.cumulative_return), (
            f"nav_series[{i}].cumulative_return 非有限: {p.cumulative_return}"
        )


def test_portfolio_drawdown_and_trades_finite(portfolio_resp):
    """drawdown / trades 数值必须有限（覆盖对称修复的另一组出口）。"""
    for i, d in enumerate(portfolio_resp.drawdown_series):
        assert math.isfinite(d.drawdown), f"drawdown_series[{i}].drawdown 非有限: {d.drawdown}"
    for i, t in enumerate(portfolio_resp.trades):
        assert math.isfinite(t.price), f"trades[{i}].price 非有限: {t.price}"
        assert math.isfinite(t.cost), f"trades[{i}].cost 非有限: {t.cost}"


def test_portfolio_sse_result_frame_valid_json(portfolio_resp):
    """模拟 SSE result 帧：allow_nan=False 必须不抛且不含 NaN/Infinity 字面。"""
    payload = jsonable_encoder({"type": "result", "data": portfolio_resp})
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in serialized, "portfolio result 帧含字面 NaN，前端 JSON.parse 必失败"
    assert "Infinity" not in serialized, "portfolio result 帧含字面 Infinity"
