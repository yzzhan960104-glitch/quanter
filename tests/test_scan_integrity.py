# -*- coding: utf-8 -*-
"""scan_integrity CLI 单测（规则 1 全市场连续性扫描）。

核心逻辑（find_gaps / load_suspend_intervals / fetch_trade_days）已在 test_integrity.py 覆盖，
本测试验证 CLI 接线：读 parquet → 组装三件套 → 输出报告 dict（unjustified 漏采标的清单）。

端到端 mock 红线：不触网、不依赖真实 data_lake——tmp_path 写 mock parquet +
monkeypatch _fetch_year_trade_cal 返固定交易日集，验证 scan 串起来正确报漏采。
"""
from __future__ import annotations

import pandas as pd


def _write_mock_daily(tmp_path, rows):
    """写 mock a_shares_daily.parquet。rows = [(date, symbol), ...]。"""
    dates = [pd.Timestamp(d) for d, _ in rows]
    syms = [s for _, s in rows]
    n = len(rows)
    df = pd.DataFrame(
        {"open": range(n), "high": range(n), "low": range(n),
         "close": range(n), "volume": range(n)},
        index=pd.MultiIndex.from_arrays([dates, syms], names=["date", "symbol"]),
    )
    df.to_parquet(tmp_path / "a_shares_daily.parquet")


def test_scan_reports_unjustified_gap(tmp_path, monkeypatch):
    """mock daily（000001.SZ 缺 09-04/09-05）+ 无 suspend → scan 报 1 个 unjustified gap。"""
    from data.tools import scan_integrity
    # mock trade_cal 避免触网（覆盖 2024-09 第一周交易日）
    monkeypatch.setattr("data.integrity._fetch_year_trade_cal", lambda y: (
        ["2024-09-02", "2024-09-03", "2024-09-04", "2024-09-05",
         "2024-09-06", "2024-09-09"] if y == 2024 else []
    ))
    _write_mock_daily(tmp_path, [
        ("2024-09-02", "000001.SZ"), ("2024-09-03", "000001.SZ"),
        ("2024-09-06", "000001.SZ"), ("2024-09-09", "000001.SZ"),  # 缺 09-04, 09-05
    ])
    # 不写 suspend_d.parquet → scan 应容错为「无停牌」

    report = scan_integrity.scan(lake_dir=str(tmp_path))

    assert report["unjustified_gaps"] >= 1
    assert "000001.SZ" in report["unjustified_symbols"]
    # 找到 000001.SZ 的漏采段
    sym_gaps = [g for g in report["gaps"] if g["symbol"] == "000001.SZ"
                and not g["suspend_justified"]]
    assert sym_gaps, "000001.SZ 应有 unjustified gap"
    assert set(sym_gaps[0]["missing_dates"]) == {"2024-09-04", "2024-09-05"}


def test_scan_symbol_filter(tmp_path, monkeypatch):
    """--symbol 过滤：只报指定标的的缺口。"""
    from data.tools import scan_integrity
    monkeypatch.setattr("data.integrity._fetch_year_trade_cal", lambda y: (
        ["2024-09-02", "2024-09-03", "2024-09-04", "2024-09-05", "2024-09-06"] if y == 2024 else []
    ))
    _write_mock_daily(tmp_path, [
        ("2024-09-02", "000001.SZ"), ("2024-09-06", "000001.SZ"),  # 缺 09-03,04,05
        ("2024-09-02", "600000.SH"), ("2024-09-06", "600000.SH"),  # 也缺，但被 filter 排除
    ])
    report = scan_integrity.scan(lake_dir=str(tmp_path), symbol="000001.SZ")
    syms_in_report = {g["symbol"] for g in report["gaps"]}
    assert syms_in_report == {"000001.SZ"}
