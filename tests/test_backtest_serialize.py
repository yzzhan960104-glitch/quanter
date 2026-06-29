"""OHLCV / positions 透传单测（针对抽取 helper，避开重型回测引擎）。

Why 直接测 helper：_serialize_backtest_result 与 run_single_backtest 串联了
BacktestEngine（CPU 密集 + 宏观取数），单测里走全链路既慢又脆。helper 是纯
序列化函数（DataFrame → Pydantic 模型），单独锁定其边界行为即可覆盖透传契约。
"""
import pandas as pd
import pytest

from server.services.backtest_service import _extract_ohlcv, _extract_positions


def test_extract_ohlcv_from_price_data():
    """从 price_data 透传 OHLCV：列名小写、日期 ISO 格式化、行序保留。"""
    df = pd.DataFrame(
        {
            "open": [10.0, 10.2],
            "high": [10.5, 10.6],
            "low": [9.8, 10.0],
            "close": [10.2, 10.4],
            "volume": [1000, 1500],
        },
        index=pd.DatetimeIndex(
            ["2024-01-02", "2024-01-03"], tz="Asia/Shanghai", name="date"
        ),
    )
    out = _extract_ohlcv({"000001.SZ": df})
    assert len(out) == 2
    # 列名映射 + 行序保留
    assert out[0].open == 10.0 and out[1].volume == 1500
    # 日期格式化为 ISO（容忍 tz 信息，断言前缀即可）
    assert out[0].date.startswith("2024-01-02")


def test_extract_ohlcv_empty_when_no_data():
    """price_data 为空 dict 时返回 []（空数据短路，防范 KeyError）。"""
    assert _extract_ohlcv({}) == []


def test_extract_positions_from_last_record():
    """末态持仓：取末行 position / position_value，仅产出 1 行快照。"""
    daily = pd.DataFrame(
        {
            "nav": [1.0e6, 1.01e6],
            "position": [0, 100],
            "position_value": [0.0, 1020.0],
            "price": [10.2, 10.2],
        },
        index=pd.DatetimeIndex(["2024-01-02", "2024-01-03"], tz="Asia/Shanghai"),
    )
    out = _extract_positions(daily, symbol="000001.SZ")
    assert len(out) == 1                       # 仅末态快照
    assert out[0].symbol == "000001.SZ"
    assert out[0].qty == 100
    assert out[0].market_value == 1020.0       # 优先取 position_value


def test_extract_positions_falls_back_to_qty_times_price():
    """position_value 列缺失时，用 position * price 兜底（防御历史 daily 结构）。"""
    daily = pd.DataFrame(
        {
            "position": [100],
            "price": [10.2],
        },
        index=pd.DatetimeIndex(["2024-01-03"], tz="Asia/Shanghai"),
    )
    out = _extract_positions(daily, symbol="X")
    assert len(out) == 1
    assert out[0].qty == 100
    # position*price 兜底路径含浮点累积误差（100*10.2=1019.9999...），用 approx 容忍
    assert out[0].market_value == pytest.approx(1020.0, rel=1e-9)


def test_extract_positions_empty_when_no_records():
    """daily_records 为空时返回 []（回测早返回 / 空数据场景）。"""
    assert _extract_positions(pd.DataFrame(), symbol="X") == []


def test_extract_positions_empty_when_flat():
    """末态 qty=0 且 market_value=0（清仓 / 从未建仓）返回 []。"""
    daily = pd.DataFrame(
        {
            "position": [0],
            "position_value": [0.0],
            "price": [10.0],
        },
        index=pd.DatetimeIndex(["2024-01-03"], tz="Asia/Shanghai"),
    )
    assert _extract_positions(daily, symbol="X") == []
