# -*- coding: utf-8 -*-
"""amihud 触发式信号过滤测试（2026-08-29 v2 · 直上 NECK 部署）。

被测对象是【组装产物】emquant/emquant_neckline_pilot.py（同 test_state_and_gates
范式）。v2 语义（触发式·策略自含）：len(signals)<min_signals 或有效值不足 →
全保留豁免；否则组内升序分位 ≤ pct_line 剔除。fetch_amihud60 的真实拉取路径
周一实盘首验（fake_gm 全链由 events_orchestration 家族覆盖），本文件钉死：
  ① apply_amihud_filter 纯函数三态（豁免/剔除/无值保留）+ 分位数学与 pandas
     rank(pct=True) 同义（防手写排序漂移）；
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
def test_exempt_when_few_signals(pilot):
    kept, dropped, exempt = pilot.apply_amihud_filter(
        _sigs("A", "B", "C"), {"A": 1.0, "B": 2.0, "C": 3.0}, 0.40, 4)
    assert exempt and not dropped and len(kept) == 3


def test_exempt_when_valid_values_insufficient(pilot):
    kept, dropped, exempt = pilot.apply_amihud_filter(
        _sigs("A", "B", "C", "D", "E"), {"A": 1.0, "B": None}, 0.40, 4)
    assert exempt and len(kept) == 5


def test_pool_percentile_drop_and_keep(pilot):
    # 5 信号：amihud 升序 A<B<C<D<E → 分位 0.2/0.4/0.6/0.8/1.0；
    # ≤0.40 剔 A、B；无值 F 保留
    vals = {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0, "E": 5.0, "F": None}
    kept, dropped, exempt = pilot.apply_amihud_filter(
        _sigs("A", "B", "C", "D", "E", "F"), vals, 0.40, 4)
    assert not exempt
    assert [s.symbol for s in kept] == ["C", "D", "E", "F"]
    assert dropped == [("A", 0.2), ("B", 0.4)]


def test_percentile_matches_pandas_rank(pilot):
    import numpy as np
    import pandas as pd
    syms = [f"S{i}" for i in range(9)]
    vals = {s: float((i * 37) % 11) for i, s in enumerate(syms)}  # 打乱序
    _, dropped, _ = pilot.apply_amihud_filter(_sigs(*syms), vals, 0.40, 4)
    ser = pd.Series(vals)
    ref = {s: round(float(p), 4) for s, p in ser.rank(pct=True).items()
           if p <= 0.40}
    assert dict(dropped) == ref


# ── ② §0 规格常量与组装器镜像 ─────────────────────────────────────
def test_sec0_filter_cfg_matches_builder(pilot):
    from emquant.build_pilot import AMIHUD_FILTER_CFG
    assert pilot.AMIHUD_FILTER == AMIHUD_FILTER_CFG
    assert pilot.AMIHUD_FILTER["enabled"] is True
    assert pilot.AMIHUD_FILTER["pct_line"] == pytest.approx(0.40)
    assert pilot.AMIHUD_FILTER["min_signals"] == 4
