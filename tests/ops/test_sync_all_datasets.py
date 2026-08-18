# -*- coding: utf-8 -*-
"""每日全数据集尝试同步测试（2026-08-19 · 用户裁决「所有数据每日尝试更新」）。

契约：
1. sweep_orphan_sentinels 清 .syncing 孤儿、保留 .failed（面板失败原因不丢）；
2. run_all 跳过 daily（STEPS 已采）与无 script 集；成功清哨兵、失败/超时写 .failed；
3. sync_tushare.py 命令注入位置参数 key（C-9 A1 同款）；
4. pipeline 尾部 spawn 为 fire-and-forget（不阻塞台账 done，异常不外溢）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import ops.sync_all_datasets as sad


@pytest.fixture
def sentinel_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sad, "SYNCING_DIR", tmp_path / ".syncing")
    sad.SYNCING_DIR.mkdir(parents=True)
    return sad.SYNCING_DIR


def test_sweep_clears_syncing_keeps_failed(sentinel_dir):
    (sentinel_dir / "margin").write_text("旧", encoding="utf-8")
    (sentinel_dir / "moneyflow").write_text("旧", encoding="utf-8")
    (sentinel_dir / "share_float.failed").write_text("上次失败原因", encoding="utf-8")
    n = sad.sweep_orphan_sentinels()
    assert n == 2
    assert not (sentinel_dir / "margin").exists()
    assert (sentinel_dir / "share_float.failed").exists()   # .failed 保留


def test_run_all_skips_daily_and_scriptless(sentinel_dir, monkeypatch):
    from config import DATASET_REGISTRY
    monkeypatch.setattr(sad, "SKIP_KEYS", {"daily"})
    called = []

    class _Proc:
        returncode = 0
        stderr = stdout = ""

    def fake_run(cmd, **kw):
        called.append(cmd[2] if len(cmd) > 2 else cmd)
        return _Proc()

    with patch.object(sad.subprocess, "run", side_effect=fake_run):
        ok, fail = sad.run_all()
    assert fail == 0 and ok > 0
    assert "daily" not in called                              # STEPS 已采
    assert "index_daily" in called                            # 其余全跑
    assert ok + len(sad.SKIP_KEYS) <= len(DATASET_REGISTRY) + 1
    # 全部成功 → 无 syncing 残留、无 .failed
    assert not list(sentinel_dir.iterdir())


def test_run_all_failure_writes_failed_sentinel(sentinel_dir):
    class _Proc:
        returncode = 3
        stderr = "Tushare: 每分钟最多访问该接口 500 次"
        stdout = ""

    with patch.object(sad.subprocess, "run", return_value=_Proc()):
        ok, fail = sad.run_all()
    assert fail >= 1 and ok + fail > 0
    failed = list(sentinel_dir.glob("*.failed"))
    assert failed, "失败集必须写 .failed 供面板展示原因"
    assert "每分钟最多访问" in failed[0].read_text(encoding="utf-8")[:200]
    # 失败后 syncing 哨兵应清（状态机单值：.failed 优先）
    assert not [p for p in sentinel_dir.iterdir() if not p.name.endswith(".failed")]


def test_build_cmd_injects_key_for_sync_tushare():
    cmd = sad._build_cmd("margin", {"script": "data/tools/sync_tushare.py"})
    assert cmd[2] == "margin"    # C-9 A1：sync_tushare.py 需位置参数 key
    cmd2 = sad._build_cmd("macro", {"script": "data/tools/sync_macro_credit.py"})
    assert len(cmd2) == 2        # 无 argparse 脚本不注入


def test_pipeline_spawns_sync_fire_and_forget():
    """pipeline_then_eod 尾部 spawn 全数据集同步（fire-and-forget，台账 done 后）。"""
    import asyncio
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock, patch
    from trading.orchestrate.pipeline import pipeline_then_eod
    from data.freshness import FreshnessResult

    today = datetime.now().strftime("%Y-%m-%d")
    with patch("trading.orchestrate.pipeline.is_trading_day", return_value=True),          patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse,          patch("trading.orchestrate.pipeline.resolve_active", return_value=[]),          patch("trading.orchestrate.pipeline.check_freshness",
               return_value=FreshnessResult("daily", True, today, today, "PASS")),          patch("trading.orchestrate.pipeline._scan_and_spawn_repair", return_value=0),          patch("trading.orchestrate.pipeline._spawn_all_datasets_sync") as spawn:
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        eng = MagicMock(); eng._eod = AsyncMock()
        asyncio.run(pipeline_then_eod(eng))
        eng._eod.assert_awaited_once()
        spawn.assert_called_once()   # brief 后必 spawn 一次（fire-and-forget）
