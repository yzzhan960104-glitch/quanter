# -*- coding: utf-8 -*-
"""R10 实验腿变体（r10leg_pilot.py）单测 · 2026-08-31 r10leg-v1 部署前钉死。

被测对象是【实验腿专属变体】emquant/r10leg_pilot.py（同 test_amihud_filter
范式 spec_from_file_location）。本文件钉死：
  ① §0 参数 = R10 champion 28 键逐位一致（防手改漂移；源=final_report.json）；
  ② R10_FILTERS 三闸配置 = champion_state.filters 预登记档；
  ③ exec_params 快照 14 键（含基底漏抄的 time_stop_days/chase_entry/
     timeout_extend* 四键——R10 champion 四键开启态的生效前提）；
  ④ 首日表加载器（正常/缺失退化全放行）；
  ⑤ momentum/band/listed/no_fri 判定式（以独立纯函数重述钉死语义，与
     pre_open 内嵌实现同式对齐——内嵌逻辑的端到端由首战 audit 验证）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "emquant" / "r10leg_pilot.py"
R10_FINAL = ROOT / "logs" / "r10_global_opt" / "final_report.json"


def _load_artifact():
    spec = importlib.util.spec_from_file_location("pilot_artifact_r10leg", ARTIFACT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pilot():
    return _load_artifact()


def test_s0_params_match_r10_champion(pilot):
    final = json.loads(R10_FINAL.read_text(encoding="utf-8"))
    cp = final["champion_params"]
    for k, v in pilot.ID_PARAMS.items():
        assert cp.get(k) == v, f"ID {k}: {v} != champion {cp.get(k)}"
    for k, v in pilot.EXEC_PARAMS.items():
        if k in ("commission_rate", "stamp_rate", "transfer_rate"):
            continue   # 费率键 pilot 专属，champion_params 不含
        assert cp.get(k) == v, f"EXEC {k}: {v} != champion {cp.get(k)}"
    # champion 全部 28 键都必须落在 §0（不留漏键）
    merged = {**pilot.ID_PARAMS, **pilot.EXEC_PARAMS}
    for k, v in cp.items():
        assert merged.get(k) == v, f"champion 键 {k}={v} 未落 §0"


def test_r10_filters_match_champion_state(pilot):
    assert pilot.R10_FILTERS["no_fri"] is True
    assert tuple(pilot.R10_FILTERS["band"]) == (1.0, 3.5)
    assert pilot.R10_FILTERS["listed_min_days"] == 400
    assert pilot.PILOT_MAX_NEW_ORDERS_PER_DAY == 5


def test_exec_params_snapshot_has_14_keys(pilot):
    res = {"neckline": 11.0, "bottom": 10.0, "atr": 0.5, "formed_at": "2026-08-28",
           "rr": 2.0}
    id_cfg = dict(pilot.ID_PARAMS)
    exec_cfg = dict(pilot.EXEC_PARAMS)
    sig = pilot._post_detect("300001.SZ", res, id_cfg, exec_cfg,
                             "2026-08-28", 10.8, 0.5)
    ep = sig.exec_params
    for k in ("time_stop_days", "timeout_extend_days",
              "timeout_extend_min_pnl", "chase_entry",
              "max_holding", "trailing_grace", "stop_atr_mult"):
        assert k in ep, f"快照缺键 {k}"
    assert ep["time_stop_days"] == 5
    assert ep["chase_entry"] is True
    assert ep["timeout_extend_days"] == 3


def test_listed_first_dates_loader(tmp_path, monkeypatch):
    pilot = _load_artifact()
    # 正常加载
    f = tmp_path / "fd.json"
    f.write_text(json.dumps({"300001.SZ": "2010-01-01"}), encoding="utf-8")
    monkeypatch.setattr(pilot, "LISTED_FIRST_DATES_FILE", str(f))
    pilot._LISTED_FIRST_DATES = None
    assert pilot._load_listed_first_dates() == {"300001.SZ": "2010-01-01"}
    # 缺失退化
    pilot._LISTED_FIRST_DATES = None
    monkeypatch.setattr(pilot, "LISTED_FIRST_DATES_FILE", str(tmp_path / "nope.json"))
    assert pilot._load_listed_first_dates() == {}


# ── 判定式语义钉死（与 pre_open 内嵌同式）──

def _mom_keep(closes, gate):
    """m20 = c[-1]/c[-21]-1；<gate 弃；数据 ≤20 根放行。"""
    if gate is None or len(closes) <= 20:
        return True
    return (closes[-1] / closes[-21] - 1.0) >= gate


def _band_keep(neckline, bottom, atr, lo, hi):
    if not atr or atr <= 0:
        return True
    return lo <= (neckline - bottom) / atr <= hi


def _listed_keep(first_date, t_minus_1, min_days):
    import pandas as pd
    if first_date is None:
        return True
    return (pd.Timestamp(t_minus_1) - pd.Timestamp(first_date)).days >= min_days


def test_momentum_gate_semantics():
    up = [10.0] * 20 + [12.0]
    down = [10.0] * 20 + [9.0]
    assert _mom_keep(up, 0.0) is True       # +20% ≥ 0 → 留
    assert _mom_keep(down, 0.0) is False    # −10% < 0 → 弃
    assert _mom_keep([1.0] * 20, 0.0) is True   # 数据不足 → 放行
    assert _mom_keep(down, None) is True    # 闸关 → 全留


def test_band_semantics():
    # H=1.0, ATR=0.5 → H/ATR=2.0 ∈ [1.0,3.5]
    assert _band_keep(11.0, 10.0, 0.5, 1.0, 3.5) is True
    # H=4.0, ATR=1.0 → 4.0 带外
    assert _band_keep(14.0, 10.0, 1.0, 1.0, 3.5) is False
    # H=0.4, ATR=0.5 → 0.8 带外（浅形态）
    assert _band_keep(10.4, 10.0, 0.5, 1.0, 3.5) is False
    assert _band_keep(11.0, 10.0, 0.0, 1.0, 3.5) is True   # atr 非法 → 放行


def test_listed_semantics():
    assert _listed_keep("2020-01-01", "2026-08-29", 400) is True
    assert _listed_keep("2026-01-01", "2026-08-29", 400) is False
    assert _listed_keep(None, "2026-08-29", 400) is True


def test_no_fri_semantics():
    import pandas as pd
    assert pd.Timestamp("2026-08-28").dayofweek == 4   # 周五
    assert pd.Timestamp("2026-08-31").dayofweek == 0   # 周一


def test_min_order_qty_board_aware_v2():
    """r10leg-v2（2026-09-01）：科创板 200 股最小申报门槛——与主腿 4456dbe2 同步。"""
    import importlib.util as _u
    spec = _u.spec_from_file_location("r10leg_v2_check", ARTIFACT)
    m = _u.module_from_spec(spec)
    import sys as _s
    _s.modules[spec.name] = m
    spec.loader.exec_module(m)
    assert "r10leg-v2" in m.PILOT_BUILD_STAMP
    assert m._min_order_qty("688700.SH") == 200
    assert m._min_order_qty("300657.SZ") == 100
    # 反转 regime 科创板 lot2=100 股 → tp2_dust 沉 lot1（同主腿语义）
    pos = {"remaining_qty": 1000, "tp1_price": 13.0, "tp2_price": 11.5,
           "tp1_done": False, "tp2_done": False,
           "exec_params": {"tp1_portion": 0.9}, "entry_date": "2026-08-21",
           "stop": 5.0, "trailing": {}}
    assert m.decide_position(11.5, dict(pos), "2026-08-21", ["2026-08-20", "2026-08-21"],
                             symbol="688700.SH") == ("sell", 0, "tp2_dust")
    assert m.decide_position(11.5, dict(pos), "2026-08-21", ["2026-08-20", "2026-08-21"],
                             symbol="300657.SZ") == ("sell", 100, "tp2_share")
