"""BacktestResponse 新增 ohlcv / positions 字段的契约测试。

目的：锁定单资产响应 schema 中 K 线序列与末态持仓快照两字段的契约，
防止后续重构误删字段或字段类型漂移（前端 ProChart / PositionsTable 强依赖）。
"""
from server.schemas.backtest import BacktestResponse, OhlcvPoint, PositionRow


def test_backtest_response_accepts_ohlcv_and_positions():
    """BacktestResponse 可接收并回读 ohlcv / positions 两个字段。"""
    resp = BacktestResponse(
        metrics={
            "initial_capital": 1e6,
            "final_nav": 1.1e6,
            "total_return": 0.1,
            "annual_return": 0.1,
            "annual_volatility": 0.15,
            "max_drawdown": -0.05,
            "sharpe_ratio": 1.2,
            "calmar_ratio": 2.0,
            "win_rate": 0.6,
            "profit_loss_ratio": 1.5,
            "n_trades": 10,
            "n_failed_trades": 1,
        },
        nav_series=[],
        drawdown_series=[],
        trades=[],
        ohlcv=[OhlcvPoint(date="2024-01-02", open=10.0, high=10.5, low=9.8, close=10.2, volume=100000)],
        positions=[PositionRow(symbol="000001.SZ", qty=100, market_value=1020.0)],
    )
    # ohlcv 字段可读，且数值未被污染
    assert resp.ohlcv[0].close == 10.2
    assert resp.ohlcv[0].date == "2024-01-02"
    # positions 字段可读，symbol 透传无截断
    assert resp.positions[0].symbol == "000001.SZ"
    assert resp.positions[0].market_value == 1020.0


def test_backtest_response_ohlcv_positions_default_empty():
    """未显式传入时，ohlcv / positions 应回退为空列表（向后兼容空数据场景）。"""
    resp = BacktestResponse(
        metrics={
            "initial_capital": 1e6,
            "final_nav": 1e6,
            "total_return": 0.0,
            "annual_return": 0.0,
            "annual_volatility": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            "calmar_ratio": 0.0,
            "win_rate": 0.0,
            "profit_loss_ratio": 0.0,
            "n_trades": 0,
            "n_failed_trades": 0,
        },
        nav_series=[],
        drawdown_series=[],
        trades=[],
    )
    assert resp.ohlcv == []
    assert resp.positions == []


def test_backtest_response_has_benchmark_series_field():
    """BacktestResponse 必须含 benchmark_series 字段（默认空列表，向后兼容旧响应）。"""
    from server.schemas.backtest import BenchmarkPoint

    # 最小合法响应（benchmark_series 缺省 → 空列表）
    resp = BacktestResponse(
        metrics={
            "initial_capital": 1e6, "final_nav": 1.0, "total_return": 0.0,
            "annual_return": 0.0, "annual_volatility": 0.0, "max_drawdown": 0.0,
            "sharpe_ratio": 0.0, "calmar_ratio": 0.0, "win_rate": 0.0,
            "profit_loss_ratio": 0.0, "n_trades": 0, "n_failed_trades": 0,
        },
        nav_series=[], drawdown_series=[], trades=[], ohlcv=[], positions=[],
    )
    assert resp.benchmark_series == []  # 缺省空列表

    # 显式构造基准节点
    bp = BenchmarkPoint(date="2024-01-02", nav=1.0)
    assert bp.date == "2024-01-02" and bp.nav == 1.0
