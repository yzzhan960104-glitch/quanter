# -*- coding: utf-8 -*-
"""amihud keep-top 信号过滤测试（2026-08-29 v3 · keep_top=5 部署）。

被测对象是【组装产物】emquant/emquant_neckline_pilot.py（同 test_state_and_gates
范式）。v3 语义（用户裁决 keep-top）：当日信号 > keep_top → amihud60 降序留前
keep_top（高=最不流动=质量侧），其余剔；≤ keep_top 或有效值 < keep_top → 全保留
豁免。fetch_amihud60 真实拉取周一实盘首验，本文件钉死：
  ① apply_amihud_filter 三态（豁免×2 / keep-top 选择 / 无值在超额日先剔）；
  ② §0 AMIHUD_FILTER 与 build_pilot.AMIHUD_FILTER_CFG 逐位一致（防手改漂移）。
"""
from __future__ import annotations

import importlib.util
import sys
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


def _sigs(*syms):
    return [SimpleNamespace(symbol=s) for s in syms]


# ── ① apply_amihud_filter 三态 ────────────────────────────────────
def test_exempt_when_within_capacity(pilot):
    kept, dropped, exempt = pilot.apply_amihud_filter(
        _sigs("A", "B", "C", "D", "E"), {"A": 1.0}, 5)
    assert exempt and not dropped and len(kept) == 5


def test_exempt_when_valid_values_insufficient(pilot):
    kept, dropped, exempt = pilot.apply_amihud_filter(
        _sigs("A", "B", "C", "D", "E", "F", "G"), {"A": 1.0, "B": None}, 5)
    assert exempt and len(kept) == 7


def test_keep_top_by_amihud_desc(pilot):
    # 8 信号、amihud：C 最高（最不流动=质量最高）… 留前 5 = C,E,A,D,G；
    # 无值 H 与低值 B 被剔
    vals = {"A": 3.0, "B": 0.5, "C": 9.0, "D": 2.5, "E": 7.0, "F": 1.0,
            "G": 2.0, "H": None}
    kept, dropped, exempt = pilot.apply_amihud_filter(
        _sigs("A", "B", "C", "D", "E", "F", "G", "H"), vals, 5)
    assert not exempt
    assert sorted(s.symbol for s in kept) == ["A", "C", "D", "E", "G"]
    assert dict(dropped) == {"B": 0.5, "F": 1.0, "H": None}


# ── ② §0 规格常量与组装器镜像 ─────────────────────────────────────
def test_sec0_filter_cfg_matches_builder(pilot):
    from emquant.build_pilot import AMIHUD_FILTER_CFG
    assert pilot.AMIHUD_FILTER == AMIHUD_FILTER_CFG
    assert pilot.AMIHUD_FILTER["enabled"] is True
    assert pilot.AMIHUD_FILTER["keep_top"] == 5
