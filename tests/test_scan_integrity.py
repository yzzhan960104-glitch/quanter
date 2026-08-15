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


# ============================================================================
# N1 停牌真值（2026-08-16）：在场计数喂入（长洞市场共识）+ unfillable sidecar 排除
# ============================================================================

# 长窗交易日（连续 3 周 15 日）：启发式需 ≥10 日长洞
_LONG_DAYS = [
    "2024-09-02", "2024-09-03", "2024-09-04", "2024-09-05", "2024-09-06",
    "2024-09-09", "2024-09-10", "2024-09-11", "2024-09-12", "2024-09-13",
    "2024-09-16", "2024-09-17", "2024-09-18", "2024-09-19", "2024-09-20",
]


def test_scan_suspects_long_hole_with_healthy_market(tmp_path, monkeypatch):
    """端到端：12 日洞 + 市场每日在场 → suspend_suspected_segments=1、unjustified=0。

    scan 从湖自身算日级在场计数喂 find_gaps（不新增数据源）——000670.SZ 实证
    形态的接线验证：市场健康而唯独该标的无数据 → 停牌推定，单列计数不混入
    unjustified（pipeline/daemon 消费侧语义不变）。
    """
    from data.tools import scan_integrity
    monkeypatch.setattr("data.integrity._fetch_year_trade_cal",
                        lambda y: _LONG_DAYS if y == 2024 else [])
    # 分母下限降到 5（生产默认 1000 对应 2017+ 全市场；单测市场只造 5 标的）
    monkeypatch.setattr("data.integrity.SUSPEND_SUSPECT_MIN_MARKET_PRESENCE", 5)
    hole = _LONG_DAYS[2:14]  # 12 个连续交易日洞
    rows = [(d, "000670.SZ") for d in [_LONG_DAYS[0], _LONG_DAYS[1], _LONG_DAYS[14]]]
    # 市场标的：15 日全在场（每日在场数 5，中位数 5 ≥ 下限 5，恒 ≥ 5×0.8）
    rows += [(d, f"60000{i}.SH") for i in range(5) for d in _LONG_DAYS]
    _write_mock_daily(tmp_path, rows)
    # 不写 suspend_d.parquet → 洞完全无 S 记录（启发式是唯一解释源）

    report = scan_integrity.scan(lake_dir=str(tmp_path))

    assert report["suspend_suspected_segments"] == 1
    assert report["unjustified_gaps"] == 0
    assert report["unjustified_symbols"] == []
    g = [g for g in report["gaps"] if g["symbol"] == "000670.SZ"][0]
    assert g["suspend_justified"] is True
    assert g["suspend_suspected"] is True
    assert g["unjustified_days"] == []


def test_scan_excludes_unfillable_marked_days(tmp_path, monkeypatch):
    """unfillable sidecar：已标记不可补的日不计入 unjustified（scan/repair 同源）。

    repair 拉过「源零行」的段记 sidecar 后，scan 不再把它算漏采——否则
    pipeline 每夜照旧 spawn repair、daemon fail-closed 闸永久拒跑。
    """
    import json
    from data.tools import scan_integrity
    monkeypatch.setattr("data.integrity._fetch_year_trade_cal", lambda y: (
        ["2024-09-02", "2024-09-03", "2024-09-04", "2024-09-05", "2024-09-06"] if y == 2024 else []
    ))
    _write_mock_daily(tmp_path, [
        ("2024-09-02", "000921.SZ"), ("2024-09-06", "000921.SZ"),  # 缺 09-03,04,05
    ])
    # 预写 sidecar：09-03~09-05 已标记不可补（盲区年代/停牌残留）
    (tmp_path / ".repair_unfillable.json").write_text(json.dumps(
        {"entries": [{"symbol": "000921.SZ", "start": "2024-09-03",
                      "end": "2024-09-05", "reason": "market_empty", "count": 3}]},
        ensure_ascii=False), encoding="utf-8")

    report = scan_integrity.scan(lake_dir=str(tmp_path))

    assert report["unjustified_gaps"] == 0, "标记日不算漏采"
    assert report["unjustified_symbols"] == []
    assert report["unfillable_skipped_segments"] == 1


def test_scan_splits_unfillable_confirmed_vs_probed(tmp_path, monkeypatch):
    """N1b：实证（market_empty/symbol_absent）与推定（probe_zero_day）分开计数。

    两类 reason 都从 unjustified 扣减（daemon fail-closed 闸同等放行），但报告
    单列 unfillable_confirmed_segments / unfillable_probed_segments——诚实呈现
    「全段实证」vs「中点采样推定」的证据等级差异，可审计可回溯。
    """
    import json
    from data.tools import scan_integrity
    monkeypatch.setattr("data.integrity._fetch_year_trade_cal", lambda y: (
        ["2024-09-02", "2024-09-03", "2024-09-04", "2024-09-05", "2024-09-06"] if y == 2024 else []
    ))
    # 两标的各缺 09-03~09-05
    _write_mock_daily(tmp_path, [
        ("2024-09-02", "000921.SZ"), ("2024-09-06", "000921.SZ"),
        ("2024-09-02", "000670.SZ"), ("2024-09-06", "000670.SZ"),
    ])
    # sidecar：000921.SZ 为 --auto 全段实证（market_empty）；000670.SZ 为 --classify
    # 中点采样推定（probe_zero_day）
    (tmp_path / ".repair_unfillable.json").write_text(json.dumps(
        {"entries": [
            {"symbol": "000921.SZ", "start": "2024-09-03", "end": "2024-09-05",
             "reason": "market_empty", "count": 3},
            {"symbol": "000670.SZ", "start": "2024-09-03", "end": "2024-09-05",
             "reason": "probe_zero_day", "count": 1},
        ]},
        ensure_ascii=False), encoding="utf-8")

    report = scan_integrity.scan(lake_dir=str(tmp_path))

    # 两类 reason 同等从 unjustified 扣减（闸放行），但分开计数（证据分级）
    assert report["unjustified_gaps"] == 0, "推定与实证都扣减 unjustified"
    assert report["unjustified_symbols"] == []
    assert report["unfillable_confirmed_segments"] == 1, "market_empty/symbol_absent = 实证"
    assert report["unfillable_probed_segments"] == 1, "probe_zero_day = 采样推定"
    assert report["unfillable_skipped_segments"] == 2, "旧总口径保留（gap 级全跳过数）"
