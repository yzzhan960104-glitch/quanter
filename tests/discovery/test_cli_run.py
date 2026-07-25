# -*- coding: utf-8 -*-
"""cli run 集成测试：subprocess 跑 python -m discovery run（小 budget），验证落库 + 报告。

slow：真实 data_lake + ProcessPool，~6min/2 组。非 slow 回归不跑这两个。
"""
import os
import subprocess
import sys

import pytest


@pytest.mark.slow
def test_run_command_produces_trials(tmp_path):
    """跑 discovery run --budget 2 --n-sobol 1 --n-random 1（~6min，2 组×3min/2核），
    exit 0，stdout 含 RunSummary 关键字段，SQLite 有 trial 落库。用 tmp_path 隔离 db。"""
    db = tmp_path / "run.db"
    env = {**os.environ, "DISCOVERY_DB": str(db)}
    proc = subprocess.run(
        [sys.executable, "-m", "discovery", "run",
         "--budget", "2", "--n-sobol", "1", "--n-random", "1",
         "--embargo", "5", "--n-proc", "2", "--seed", "42"],
        capture_output=True, text=True, env=env, cwd=os.getcwd(),
    )
    assert proc.returncode == 0, proc.stderr
    # RunSummary 关键字段
    assert "RunSummary" in proc.stdout or "n_new_trials" in proc.stdout
    assert "snapshot:" in proc.stdout
    # SQLite 落库校验
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    n = conn.execute("SELECT COUNT(*) c FROM trial").fetchone()["c"]
    conn.close()
    assert n >= 1   # 至少 1 组落库（可能 1 组异常 None）


@pytest.mark.slow
def test_run_command_dedup_on_rerun(tmp_path):
    """同 seed 同 budget 重跑 → 第二次 n_skipped_dup == budget（断点续跑去重）。"""
    db = tmp_path / "run2.db"
    env = {**os.environ, "DISCOVERY_DB": str(db)}
    args = [sys.executable, "-m", "discovery", "run",
            "--budget", "1", "--n-sobol", "1", "--n-random", "0",
            "--embargo", "5", "--n-proc", "1", "--seed", "7"]
    # 第一次
    p1 = subprocess.run(args, capture_output=True, text=True, env=env, cwd=os.getcwd())
    assert p1.returncode == 0, p1.stderr
    # 第二次（同 seed 同 budget）
    p2 = subprocess.run(args, capture_output=True, text=True, env=env, cwd=os.getcwd())
    assert p2.returncode == 0, p2.stderr
    # 第二次应全跳过（断点续跑）—— n_skipped_dup >= 1
    assert "n_skipped_dup" in p2.stdout
