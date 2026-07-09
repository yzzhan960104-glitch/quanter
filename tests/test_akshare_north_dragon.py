"""AKShareClient 北向资金/龙虎榜数据源 + sync_dragon_list 符号归一单测（mock akshare，不依赖网络）。

注：原文件（test_akshare_north_dragon.py）还含 factors/alternative（北向动量/龙虎榜信号）因子测试，
蔡森专精化 Phase 1 Task 3 删除 factors 体系后随之一并移除——本文件仅保留纯数据源测试部分，
覆盖 fetch_north_flow / fetch_dragon_list（data/clients/akshare_client）与 _normalize_symbol
（scripts/sync_dragon_list），不 import 任何已删的 factors 模块。
"""
import pandas as pd

from data.clients import akshare_client


def _reset_akshare_breaker():
    """复位熔断器内部状态，避免被其它用例污染（与 test_akshare_client 同范式）。"""
    from data.resilience import CircuitState, akshare_breaker
    akshare_breaker._state = CircuitState.CLOSED
    akshare_breaker._failure_count = 0


# ---------------- AKShareClient 北向资金/龙虎榜 ----------------

def test_fetch_north_flow_cleanses_and_slices(monkeypatch):
    """fetch_north_flow：日期列→DatetimeIndex，资金流入列→north_net_flow，切片到 [start, end]。"""
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
    """akshare 返空 → 空 DF（不抛，对齐失败返空 DF 红线契约）。"""
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
    """akshare 异常 → record_failure + 返空 DF（不抛到调用方，对齐 yfinance 范式）。"""
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
    """6 位代码 → .SH/.SZ 后缀（6/9 开头上交所，其余深交所）。"""
    from scripts.sync_dragon_list import _normalize_symbol
    assert _normalize_symbol("600000") == "600000.SH"
    assert _normalize_symbol("000001") == "000001.SZ"
    assert _normalize_symbol("900001") == "900001.SH"  # 9 开头 B 股上交所
