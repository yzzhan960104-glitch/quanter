# -*- coding: utf-8 -*-
"""public_snapshot trailing 轨迹回放（P5.3）：阶梯形状与缺源降级。

不连真终端：部署产物用 tmp_path 合成最小 main.py（同 compute_stop_price
数学），日历 monkeypatch 成小窗口——验证 ①grace 段恒平 ②grace 后逐日
step×ATR 抬升 ③末点=当前止损位 ④neckline/atr 缺 → 空路径不猜。
"""
from __future__ import annotations

from pathlib import Path

from ops import public_snapshot as ps

_MAIN = '''
def compute_stop_price(neckline, atr, holding_days, stop_atr_mult, grace, step, floor):
    base_stop = neckline - stop_atr_mult * atr
    if grace and step and holding_days > grace:
        eff_mult = stop_atr_mult - (holding_days - grace) * step
        if floor is not None:
            eff_mult = max(eff_mult, floor)
        return neckline - eff_mult * atr
    return base_stop
'''

CAL = ["2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
       "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03"]


def test_trailing_path_step_shape(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text(_MAIN, encoding="utf-8")
    monkeypatch.setattr(ps, "_sh_calendar", lambda: list(CAL))
    pos = {"entry_date": "2026-08-27",
           "trailing": {"neckline": 100.0, "atr": 2.0, "stop_atr_mult": 1.5,
                        "grace": 2, "step": 0.5, "floor": None}}
    path = ps._trailing_path(tmp_path, pos)
    assert path == [["2026-08-27", 97.0], ["2026-08-28", 97.0],
                    ["2026-08-31", 97.0], ["2026-09-01", 98.0],
                    ["2026-09-02", 99.0], ["2026-09-03", 100.0]]
    # grace（2 日）内恒 base_stop 平段；之后逐日 +step×ATR 单调抬升
    flats = [v for _, v in path[:3]]
    assert len(set(flats)) == 1
    steps = [v for _, v in path[3:]]
    assert steps == sorted(steps) and len(set(steps)) == len(steps)
    # 末点=当前止损位（holding_days=5 活口径）
    assert path[-1][1] == 100.0 - (1.5 - 3 * 0.5) * 2.0


def test_trailing_path_degrades_without_source(tmp_path, monkeypatch):
    """neckline/atr 缺或部署产物不可读 → 空路径（画线缺就不画）。"""
    monkeypatch.setattr(ps, "_sh_calendar", lambda: list(CAL))
    no_tr = {"entry_date": "2026-08-27", "trailing": {}}
    assert ps._trailing_path(tmp_path, no_tr) == []
    bad_dir = tmp_path / "empty"            # 无 main.py 的策略目录
    bad_dir.mkdir()
    pos = {"entry_date": "2026-08-27",
           "trailing": {"neckline": 100.0, "atr": 2.0, "grace": 0, "step": 0.0}}
    assert ps._trailing_path(bad_dir, pos) == []


def test_sh_calendar_dedup_after_norm():
    """日历去重红线（09-03 实锤）：_norm 后必须唯一（重复日历令 holding_days 翻倍）。"""
    cal = ps._sh_calendar()
    assert len(cal) == len(set(cal))
