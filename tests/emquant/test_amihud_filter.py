# -*- coding: utf-8 -*-
"""amihud 信号过滤闸测试（2026-08-29 采纳部署 · NECK 主腿）。

被测对象是【组装产物】emquant/emquant_neckline_pilot.py（同 test_state_and_gates
范式）——§4 的 load_amihud_pct/apply_amihud_filter 两个纯/半纯单元 + §0 规格常量。
编排层接线（§6 ④' 块）依赖 fake_gm 全链路，由 test_events_orchestration 家族
的既有 harness 覆盖；本文件钉死过滤语义三件：
  ① load_amihud_pct：新鲜→dict；过期/损坏/缺失/空表→None（fail-open 统一口径）
  ② apply_amihud_filter：<line 剔除、≥line 保留、无值放行（NaN-keep 同回测）
  ③ §0 AMIHUD_FILTER 常量与 build_pilot.AMIHUD_FILTER_CFG 逐位一致（防手改漂移）
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "emquant" / "emquant_neckline_pilot.py"


def _load_artifact():
    spec = importlib.util.spec_from_file_location("pilot_artifact_amihud", ARTIFACT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pilot():
    return _load_artifact()


def _write_pct_file(path, d, pct):
    import json
    path.write_text(json.dumps({"date": d, "pct": pct}), encoding="utf-8")


# ── ① load_amihud_pct ─────────────────────────────────────────────
def test_load_fresh_file_returns_map(pilot, tmp_path):
    f = tmp_path / "amihud_pct_latest.json"
    _write_pct_file(f, date.today().isoformat(), {"300001.SZ": 0.55, "600000.SH": 0.2})
    m = pilot.load_amihud_pct(path=f)
    assert m == {"300001.SZ": 0.55, "600000.SH": 0.2}


def test_load_stale_file_returns_none(pilot, tmp_path):
    f = tmp_path / "amihud_pct_latest.json"
    stale = (date.today() - timedelta(days=pilot.AMIHUD_FILTER["max_stale_days"] + 1)
             ).isoformat()
    _write_pct_file(f, stale, {"300001.SZ": 0.55})
    assert pilot.load_amihud_pct(path=f) is None


def test_load_within_tolerance_weekend_ok(pilot, tmp_path):
    f = tmp_path / "amihud_pct_latest.json"
    edge = (date.today() - timedelta(days=pilot.AMIHUD_FILTER["max_stale_days"])
            ).isoformat()
    _write_pct_file(f, edge, {"300001.SZ": 0.55})
    assert pilot.load_amihud_pct(path=f) == {"300001.SZ": 0.55}


def test_load_corrupt_missing_empty_all_none(pilot, tmp_path):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    empty = tmp_path / "empty.json"
    _write_pct_file(empty, date.today().isoformat(), {})
    assert pilot.load_amihud_pct(path=tmp_path / "absent.json") is None
    assert pilot.load_amihud_pct(path=corrupt) is None
    assert pilot.load_amihud_pct(path=empty) is None


# ── ② apply_amihud_filter ─────────────────────────────────────────
def _sigs(*syms):
    return [SimpleNamespace(symbol=s) for s in syms]


def test_filter_drops_below_line_keeps_rest(pilot):
    kept, dropped = pilot.apply_amihud_filter(
        _sigs("A", "B", "C"), {"A": 0.39, "B": 0.40, "C": 0.95}, 0.40)
    assert [s.symbol for s in kept] == ["B", "C"]
    assert dropped == [("A", 0.39)]


def test_filter_missing_value_keep(pilot):
    kept, dropped = pilot.apply_amihud_filter(_sigs("A", "D"), {"A": 0.9}, 0.40)
    assert [s.symbol for s in kept] == ["A", "D"]
    assert dropped == []


# ── ③ §0 规格常量与组装器镜像 ─────────────────────────────────────
def test_sec0_filter_cfg_matches_builder(pilot):
    from emquant.build_pilot import AMIHUD_FILTER_CFG
    # 2026-08-29 回退：enabled=False（universe 校准错配，见 build_pilot 注释）——
    # 镜像断言只锁「产物=组装器」一致性，enabled 值由部署裁决面持有
    assert pilot.AMIHUD_FILTER == AMIHUD_FILTER_CFG
    assert pilot.AMIHUD_FILTER["pct_line"] == pytest.approx(0.40)
