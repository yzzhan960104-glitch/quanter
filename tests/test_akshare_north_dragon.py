"""AKShareClient 北向/龙虎榜 + factors/alternative 单测（mock akshare + 临时湖，不依赖网络）。"""
import pandas as pd
import pytest

from data.clients import akshare_client


def _reset_akshare_breaker():
    from data.resilience import CircuitState, akshare_breaker
    akshare_breaker._state = CircuitState.CLOSED
    akshare_breaker._failure_count = 0


# ---------------- AKShareClient 北向/龙虎榜 ----------------

def test_fetch_north_flow_cleanses_and_slices(monkeypatch):
    """fetch_north_flow：日期列→DatetimeIndex，资金流入列→north_net_flow，切片。"""
    fake = pd.DataFrame({
        "日期": ["2024-01-02", "2024-01-03", "2024-01-04"],
        "当日资金流入": [10.5, -5.2, 8.0],
    })
    import akshare
    monkeypatch.setattr(akshare, "stock_hsgt_hist_em", lambda **kw: fake, raising=False)
    _reset_akshare_breaker()

    c = akshare_client.AKShareClient()
    df = c.fetch_north_flow("2024-01-02", "2024-01-04")
    assert "north_net_flow" in df.columns
    assert len(df) == 3
    assert df["north_net_flow"].iloc[0] == 10.5
    assert df["north_net_flow"].iloc[1] == -5.2


def test_fetch_north_flow_empty_returns_empty(monkeypatch):
    """akshare 返空 → 空 DF（不抛）。"""
    import akshare
    monkeypatch.setattr(akshare, "stock_hsgt_hist_em", lambda **kw: pd.DataFrame(), raising=False)
    _reset_akshare_breaker()

    c = akshare_client.AKShareClient()
    df = c.fetch_north_flow("2024-01-02", "2024-01-04")
    assert df.empty


def test_fetch_dragon_list_passthrough(monkeypatch):
    """fetch_dragon_list 调 ak.stock_lhb_detail_daily_sina，非空透传。"""
    fake = pd.DataFrame({"代码": ["600000", "000001"], "名称": ["浦发银行", "平安银行"]})
    import akshare
    monkeypatch.setattr(akshare, "stock_lhb_detail_daily_sina", lambda **kw: fake)
    _reset_akshare_breaker()

    c = akshare_client.AKShareClient()
    df = c.fetch_dragon_list("2024-01-02")
    assert len(df) == 2
    assert "代码" in df.columns


def test_fetch_dragon_list_failure_records_breaker(monkeypatch):
    """akshare 异常 → record_failure + 返空 DF（不抛到调用方）。"""
    import akshare
    def _boom(**kw):
        raise ConnectionError("network down")
    monkeypatch.setattr(akshare, "stock_lhb_detail_daily_sina", _boom)
    _reset_akshare_breaker()

    from data.resilience import akshare_breaker
    c = akshare_client.AKShareClient()
    df = c.fetch_dragon_list("2024-01-02")
    assert df.empty
    assert akshare_breaker._failure_count == 1
    _reset_akshare_breaker()


# ---------------- sync_dragon_list 符号归一 ----------------

def test_normalize_symbol_suffix_rule():
    """6 位代码 → .SH/.SZ 后缀（6/9 开头上交所）。"""
    from scripts.sync_dragon_list import _normalize_symbol
    assert _normalize_symbol("600000") == "600000.SH"
    assert _normalize_symbol("000001") == "000001.SZ"
    assert _normalize_symbol("900001") == "900001.SH"  # 9 开头 B 股上交所


# ---------------- factors/alternative ----------------

def test_north_flow_momentum_rolling_sum(tmp_path):
    """north_flow_momentum：window 日累计净流入。"""
    from data.lake_reader import DataLakeReader
    reader = DataLakeReader.get_instance()
    reader._lakes.clear()
    reader._ffills.clear()
    reader._dtypes.clear()
    reader._default_key = None

    df = pd.DataFrame(
        {"north_net_flow": [1.0, 2.0, 3.0, 4.0, 5.0]},
        index=pd.date_range("2024-01-01", periods=5, freq="D"),
    )
    df.index.name = "date"
    path = tmp_path / "nf.parquet"
    df.to_parquet(path)
    reader.load(str(path), key="north_flow")

    from factors.alternative import north_flow_momentum
    s = north_flow_momentum("2024-01-01", "2024-01-05", window=3)
    # 滚动 3 日累计：1+2+3=6, 2+3+4=9, 3+4+5=12（前 2 个 NaN 被 dropna）
    assert s.iloc[0] == 6.0
    assert s.iloc[-1] == 12.0
    assert len(s) == 3

    reader._lakes.clear()


def test_dragon_signal_returns_symbol_set(tmp_path):
    """dragon_signal：当日上榜 symbol 集合；无该日返空 set。"""
    from data.lake_reader import DataLakeReader
    reader = DataLakeReader.get_instance()
    reader._lakes.clear()
    reader._ffills.clear()
    reader._dtypes.clear()
    reader._default_key = None

    # hit 列确保 parquet 有数据列（无列 MultiIndex parquet 读回会丢层级名）
    df = pd.DataFrame(
        {"hit": 1},
        index=pd.MultiIndex.from_tuples(
            [
                (pd.Timestamp("2024-01-02"), "600000.SH"),
                (pd.Timestamp("2024-01-02"), "000001.SZ"),
                (pd.Timestamp("2024-01-03"), "600010.SH"),
            ],
            names=["date", "symbol"],
        ),
    )
    path = tmp_path / "dragon.parquet"
    df.to_parquet(path)
    reader.load(str(path), key="dragon_list")

    from factors.alternative import dragon_signal
    assert dragon_signal("2024-01-02") == {"600000.SH", "000001.SZ"}
    assert dragon_signal("2024-01-05") == set()  # 无该日

    reader._lakes.clear()


def test_north_flow_momentum_missing_lake_returns_empty():
    """north_flow 湖未加载 → 返空 Series（离线降级）。"""
    from data.lake_reader import DataLakeReader
    reader = DataLakeReader.get_instance()
    reader._lakes.clear()
    reader._ffills.clear()
    reader._dtypes.clear()
    reader._default_key = None

    from factors.alternative import north_flow_momentum
    s = north_flow_momentum("2024-01-01", "2024-01-05")
    assert s.empty
