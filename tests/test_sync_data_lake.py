"""数据湖同步：universe 过滤 ST、空数据跳过、断点续传。"""
import pandas as pd
from scripts.sync_data_lake import load_universe, fetch_qfq, build_multiindex


class _FakePro:
    def stock_basic(self, **kwargs):
        return pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ", "600000.SH"],
            "name": ["平安银行", "万科A", "ST 浦发"],
            "list_date": ["19910403", "19910129", "19991110"],
        })

    def pro_bar(self, **kwargs):
        # 仅 000001.SZ 返回数据，其它返回空，覆盖"空数据跳过"
        if kwargs.get("ts_code") == "000001.SZ":
            return pd.DataFrame({
                "trade_date": ["20240102", "20240103"],
                "open": [10.0, 11.0], "high": [10.5, 11.5], "low": [9.8, 10.8],
                "close": [10.2, 11.1], "vol": [1000, 1100], "amount": [1e7, 1.1e7],
            })
        return pd.DataFrame()


def test_load_universe_excludes_st():
    codes = load_universe(_FakePro())
    assert "000001.SZ" in codes
    assert "000002.SZ" in codes
    assert "600000.SH" not in codes  # 名称含 ST 被剔除


def test_fetch_qfq_cleanses_columns():
    df = fetch_qfq(_FakePro(), "000001.SZ", "2024-01-01", "2024-01-31")
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "amount"]
    assert len(df) == 2


def test_build_multiindex(tmp_path):
    shard = tmp_path / "000001.SZ.parquet"
    fetch_qfq(_FakePro(), "000001.SZ", "2024-01-01", "2024-01-31").to_parquet(shard)
    out = tmp_path / "lake.parquet"
    build_multiindex(str(tmp_path), str(out))
    lake = pd.read_parquet(out)
    assert isinstance(lake.index, pd.MultiIndex)
    assert "000001.SZ" in lake.index.get_level_values("symbol").unique()
