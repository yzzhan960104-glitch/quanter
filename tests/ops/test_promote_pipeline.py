# -*- coding: utf-8 -*-
"""promote_pipeline（提案同意后换代自动化）单测：分叉检测三方比对/时窗闸/
deploy 守卫（dry-run 默认/main 拒/pending 拒）。构建与部署本体（subprocess/
文件复制）不测——18:50 watch 实跑与人工 deploy 验证。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ops import promote_pipeline as pp


# ─────────────────────── snapshot_champion_id：键路径 ───────────────────────

def test_snapshot_champion_reads_sources_path(tmp_path, monkeypatch):
    snap = tmp_path / "params_snapshot.json"
    snap.write_text(json.dumps({
        "sources": {"experiment": {
            "champion_experiment_id": "neckline_x_20260101"}}}), encoding="utf-8")
    monkeypatch.setattr(pp, "SNAPSHOT", snap)
    assert pp.snapshot_champion_id() == "neckline_x_20260101"


def test_snapshot_champion_missing_returns_none(tmp_path, monkeypatch):
    snap = tmp_path / "params_snapshot.json"
    snap.write_text(json.dumps({"sources": {}}), encoding="utf-8")
    monkeypatch.setattr(pp, "SNAPSHOT", snap)
    assert pp.snapshot_champion_id() is None
    monkeypatch.setattr(pp, "SNAPSHOT", tmp_path / "absent.json")
    assert pp.snapshot_champion_id() is None


# ─────────────────────── detect_divergence：三方比对语义 ───────────────────────

def _mk(monkeypatch, tmp_path, active, snap, deployed="2026-09-01 x"):
    monkeypatch.setattr(pp, "active_champion",
                        lambda: {"id": active, "params": {}} if active else None)
    monkeypatch.setattr(pp, "snapshot_champion_id", lambda: snap)
    monkeypatch.setattr(pp, "deployed_stamp", lambda d: deployed)

    class _L:
        key, label = "exp", "实验腿"
    import types
    fake_gc = types.ModuleType("ops.gm_ops_common")
    fake_gc.active_legs = lambda: [_L()]
    fake_gc.leg_strategy_dir = lambda leg: tmp_path
    import sys as _s
    import ops as _ops
    monkeypatch.setitem(_s.modules, "ops.gm_ops_common", fake_gc)
    monkeypatch.setattr(_ops, "gm_ops_common", fake_gc, raising=False)


def test_divergence_true_when_snapshot_lags(monkeypatch, tmp_path):
    _mk(monkeypatch, tmp_path, active="new_champ", snap="old_champ")
    d = pp.detect_divergence()
    assert d["diverged"] is True and d["active"] == "new_champ"


def test_divergence_false_when_aligned(monkeypatch, tmp_path):
    _mk(monkeypatch, tmp_path, active="champ_a", snap="champ_a")
    assert pp.detect_divergence()["diverged"] is False


def test_divergence_false_without_active(monkeypatch, tmp_path):
    _mk(monkeypatch, tmp_path, active=None, snap="old")
    d = pp.detect_divergence()
    assert d["diverged"] is False and "无 ACTIVE" in d["why"][0]


# ─────────────────────── 时窗闸 ───────────────────────

def test_trading_window_gates():
    assert pp._in_trading_window(datetime(2026, 9, 3, 10, 30)) is True    # 周四盘中
    assert pp._in_trading_window(datetime(2026, 9, 3, 9, 34)) is False    # 早于 9:35
    assert pp._in_trading_window(datetime(2026, 9, 3, 16, 0)) is False    # 盘后
    assert pp._in_trading_window(datetime(2026, 9, 5, 10, 0)) is False    # 周六
    assert pp._in_trading_window(datetime(2026, 9, 3, 12, 0)) is True     # 午间连续


# ─────────────────────── deploy 守卫（不触真构建/复制） ───────────────────────

def test_deploy_rejects_main_without_allow():
    assert pp.deploy(leg="main") == 2                     # incumbent 红线默认拒


def test_deploy_rejects_without_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "PENDING", tmp_path / "absent.json")
    assert pp.deploy(leg="exp") == 2


def test_deploy_dry_run_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pp, "PENDING", tmp_path / "pending.json")
    (tmp_path / "pending.json").write_text(json.dumps({
        "champion": "champ_x", "deployed_stamps": {"exp": "old"},
        "plan": {"steps": ["s1", "s2"]}}), encoding="utf-8")

    class _L:
        key, label = "exp", "实验腿"
    import types
    fake_gc = types.ModuleType("ops.gm_ops_common")
    fake_gc.active_legs = lambda: [_L()]
    fake_gc.leg_strategy_dir = lambda leg: tmp_path
    import sys as _s
    import ops as _ops
    monkeypatch.setitem(_s.modules, "ops.gm_ops_common", fake_gc)
    monkeypatch.setattr(_ops, "gm_ops_common", fake_gc, raising=False)

    assert pp.deploy(leg="exp", execute=False) == 0        # dry-run 零变更
    out = capsys.readouterr().out
    assert "dry-run" in out and "champ_x" in out
    assert not (tmp_path / "main.py").exists()             # 未写部署目录
