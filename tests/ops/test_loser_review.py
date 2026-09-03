# -*- coding: utf-8 -*-
"""loser_review（每日亏损持仓 LLM 归因）单测：解析/降级/K线窗口/信号回扫。

不连真 7002/z.ai：湖读数用 tmp parquet 驱动真实现（hermetic），LLM mock。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops import loser_review as lr


# ─────────────────────── _parse_llm：输出契约解析 ───────────────────────

def test_parse_llm_fenced_json():
    text = ('分析如下……```json\n{"primary": "入场时机偏晚", "secondary": "市场走弱",'
            '\n"evidence": "现价距止损 14.6%", "confidence": "高",'
            '\n"risk_state": "持有观察"}\n```\n## 复盘\n正文…')
    r = lr._parse_llm(text)
    assert r["primary"] == "入场时机偏晚"
    assert r["risk_state"] == "持有观察"
    assert r["markdown"].startswith("分析如下") and "复盘" in r["markdown"]
    assert "```json" not in r["markdown"]               # 围栏已剥


def test_parse_llm_unfenced_json():
    r = lr._parse_llm('{"primary":"x","secondary":null,"evidence":null,'
                      '"confidence":"低","risk_state":"收紧关注"}\n正文')
    assert r["risk_state"] == "收紧关注"


def test_parse_llm_bad_json_degrades_to_markdown():
    r = lr._parse_llm("模型没按格式输出，全是散文 {坏json")
    assert r["primary"] is None and r["risk_state"] is None
    assert "散文" in r["markdown"]


def test_parse_llm_risk_state_vocab_guard():
    """词表外 risk_state 置 null（前端中性灰），其余字段保留。"""
    r = lr._parse_llm('```json{"primary":"p","risk_state":"建议清仓"}```')
    assert r["risk_state"] is None
    assert r["primary"] == "p"


# ─────────────────────── analyze_one：降级不炸 ───────────────────────

def test_analyze_one_degrades_on_config_error(monkeypatch):
    from infra.llm.base import LLMConfigError

    class _Boom:
        def call(self, *a, **k):
            raise LLMConfigError("GLM_API_KEY 未配置")

    import infra.llm as illm
    monkeypatch.setattr(illm, "get_llm_client", lambda: _Boom())
    r = lr.analyze_one({"symbol": "300433.SZ"})
    assert r["markdown"] is None
    assert "LLMConfigError" in r["error"]
    assert r["risk_state"] is None


def test_analyze_one_parses_mock_llm(monkeypatch):
    class _Ok:
        def call(self, prompt, **k):
            assert "300433" in prompt or "蓝思" in prompt or "symbol" not in prompt
            return ('```json{"primary":"市场系统性","secondary":"止损偏松",'
                    '"evidence":"上证 -0.38%","confidence":"中",'
                    '"risk_state":"持有观察"}```\n# 复盘\n正文')

    import infra.llm as illm
    monkeypatch.setattr(illm, "get_llm_client", lambda: _Ok())
    r = lr.analyze_one({"symbol": "300433.SZ", "_klines": [["d", 1, 2, 3, 4, 5]]})
    assert r["primary"] == "市场系统性"
    assert "复盘" in r["markdown"]


# ─────────────────────── _klines_for / _market_pct：tmp 湖驱动 ───────────────────────

def _mk_lake(tmp_path: Path):
    import pandas as pd
    import numpy as np
    lake = tmp_path / "data_lake"
    lake.mkdir()
    days = pd.bdate_range("2026-08-01", "2026-09-03")
    n = len(days)
    rng = np.arange(n)
    px = 10 + rng * 0.1                              # 线性上行，统计可手算
    share = pd.DataFrame({"open": px - .05, "high": px + .2, "low": px - .2,
                          "close": px, "volume": 1000 + rng},
                         index=pd.DatetimeIndex(days, name="date"))
    share.index = pd.MultiIndex.from_arrays(
        [["300433.SZ"] * n, days], names=["symbol", "date"])
    (lake / "a_shares_daily.parquet").parent.mkdir(parents=True, exist_ok=True)
    share.to_parquet(lake / "a_shares_daily.parquet")

    idx = pd.DataFrame({"close": px}, index=pd.DatetimeIndex(days, name="date"))
    idx.index = pd.MultiIndex.from_arrays(
        [["000001.SH"] * n, days], names=["symbol", "date"])
    idx.to_parquet(lake / "index_daily.parquet")
    return lake, px


def test_klines_window_before_plus_all_after(tmp_path, monkeypatch):
    """进场前 20 根背景 + 进场后全部（含最新日）——窗口终点不截在进场日。"""
    import pandas as pd
    lake, _ = _mk_lake(tmp_path)
    monkeypatch.setattr(lr, "ROOT", tmp_path)
    days = pd.bdate_range("2026-08-01", "2026-09-03")
    entry = f"{days[21]:%Y-%m-%d}"                    # 8 月第 22 个交易日进场
    klines, stat = lr._klines_for("300433.SZ", entry, before=20)
    total = len(days)
    assert len(klines) == total - (21 - 20)           # 前 20 + 进场日起全部
    assert klines[-1][0] == "2026-09-03"              # 最新日在窗口内
    assert stat["high"] > stat["low"]
    assert stat["dd_pct"] <= 0                        # 上行序列：现价=高点，回撤 0


def test_market_pct_entry_to_latest(tmp_path, monkeypatch):
    lake, px = _mk_lake(tmp_path)
    monkeypatch.setattr(lr, "ROOT", tmp_path)
    import pandas as pd
    days = pd.bdate_range("2026-08-01", "2026-09-03")
    pct = lr._market_pct("000001.SH", f"{days[0]:%Y-%m-%d}")
    expect = (px[-1] / px[0] - 1) * 100
    assert pct == pytest.approx(expect, abs=0.01)


def test_market_pct_missing_entry_returns_none(tmp_path, monkeypatch):
    _mk_lake(tmp_path)
    monkeypatch.setattr(lr, "ROOT", tmp_path)
    assert lr._market_pct("000001.SH", "2030-01-01") is None


# ─────────────────────── _signal_of：audit 回扫 ───────────────────────

def test_signal_of_reads_latest(tmp_path, monkeypatch):
    import types
    fake_gc = types.ModuleType("ops.gm_ops_common")
    fake_gc.audit_csv_path = lambda day, leg_dir: (
        Path(leg_dir) / "audit" / f"audit_{day.replace('-', '')}.csv")
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "ops.gm_ops_common", fake_gc)
    import ops as _ops_pkg
    monkeypatch.setattr(_ops_pkg, "gm_ops_common", fake_gc, raising=False)

    ad = tmp_path / "audit"
    ad.mkdir()

    def _w(day: str, sym: str, neck: float, rr: float):
        # 真实 audit 由策略 csv.writer 写（JSON 串含逗号自动加引号）——
        # 手拼裸行会被 csv.reader 拆碎，夹具必须同 writer 语义
        import csv as _csv
        with (ad / f"audit_{day.replace('-', '')}.csv").open(
                "w", encoding="utf-8", newline="") as f:
            _csv.writer(f).writerow(
                [f"{day}T09:31:00", "SIGNAL",
                 json.dumps({"symbol": sym, "neckline": neck, "rr": rr})])

    _w("2026-09-01", "300433.SZ", 38.0, 2.0)
    _w("2026-09-02", "300433.SZ", 39.0, 2.4)
    (ad / "audit_20260902.csv").open("a", encoding="utf-8", newline="").write(
        f'2026-09-02T09:31:01,SIGNAL,{json.dumps({"symbol": "OTHER.SZ", "neckline": 1.0})}\n')
    sig = lr._signal_of(tmp_path, "300433.SZ")
    assert sig["neckline"] == 39.0                     # 最新日胜出
    assert sig["rr"] == 2.4
