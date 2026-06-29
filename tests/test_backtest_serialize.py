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


def test_extract_positions_safe_when_position_value_nan_or_inf():
    """position_value 为 NaN/Inf（极端行情 / 除零场景）时透传安全值（0.0），防非法 JSON。

    覆盖本分支新加的 NaN/Inf 安全化：JSON 规范不允许 NaN/Infinity，
    若 _safe_float 防护失效，NaN 会直接透传进 PositionRow → FastAPI 产出非法 JSON →
    前端 JSON.parse 崩。此处锁定 NaN/Inf → 0.0 的兜底语义。
    """
    # 末行 position_value 为 NaN（position 非零，走 position_value 优先分支）
    daily_nan = pd.DataFrame(
        {
            "nav": [1.0e6, 1.01e6],
            "position": [0, 100],
            "position_value": [0.0, float("nan")],
            "price": [10.2, 10.2],
        },
        index=pd.DatetimeIndex(["2024-01-02", "2024-01-03"], tz="Asia/Shanghai"),
    )
    out_nan = _extract_positions(daily_nan, symbol="X")
    # position=100 非零，故仍产出 1 行快照
    assert len(out_nan) == 1
    assert out_nan[0].market_value == 0.0  # NaN position_value → 安全值 0.0
    assert out_nan[0].qty == 100.0  # qty 同样走 _safe_float，正常有限值恒等

    # 末行 position_value 为 Inf（同分支）
    daily_inf = pd.DataFrame(
        {
            "position": [100],
            "position_value": [float("inf")],
            "price": [10.2],
        },
        index=pd.DatetimeIndex(["2024-01-03"], tz="Asia/Shanghai"),
    )
    out_inf = _extract_positions(daily_inf, symbol="X")
    assert len(out_inf) == 1
    assert out_inf[0].market_value == 0.0  # Inf position_value → 安全值 0.0

    # 兜底分支（position_value 列缺失）：price 为 NaN 时 qty*price 也应安全化
    daily_fallback_nan = pd.DataFrame(
        {
            "position": [100],
            "price": [float("nan")],
        },
        index=pd.DatetimeIndex(["2024-01-03"], tz="Asia/Shanghai"),
    )
    out_fb = _extract_positions(daily_fallback_nan, symbol="X")
    assert len(out_fb) == 1
    assert out_fb[0].market_value == 0.0  # qty*price 兜底路径 NaN → 安全值 0.0
    assert out_fb[0].qty == 100.0
