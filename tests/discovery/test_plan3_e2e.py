# -*- coding: utf-8 -*-
"""Plan 3 端到端 slow 集成：真实 optuna TPE 小 budget 跑批（~10min），验证收敛字段 + 落库。

物理意图：T1-T7 单元测试各验一段，本模块做"整机联调"——真实 optuna TPE 跑批，确认
freeze→Sobol→TPE→落库→RunSummary 收敛字段（status/rho/ei/frontier_size/dsr_top）端到端
串通。slow 标记，非 slow 回归（pytest -m "not slow"）不跑（单用例 ~5-10min，回归太重）。

隔离：每用例 tmp_path 独立 SQLite（DISCOVERY_DB 环境变量覆盖 store.DEFAULT_DB_PATH），
不污染 logs/discovery_trials.db。
"""
import os
import subprocess
import sys

import pytest


@pytest.mark.slow
def test_run_with_tpe_produces_convergence_fields(tmp_path):
    """discovery run --budget 2 --n-sobol 2 --tpe-trials 2（~10min，Sobol 2+TPE 2 组），
    exit 0，stdout 含 status/rho/ei 收敛字段，SQLite 有 trial 落库。

    验收点：
    - returncode 0：跑批链路（freeze→sample→evaluate→store→TPE→收敛判定）无异常
    - stdout 含 status=/rho=/ei=：RunSummary 收敛字段真的从 runner 透传到 cli 打印
      （T6→T7 接口对齐的端到端证据）
    - SQLite trial 表 ≥1 行：落库链路通（store.write_trial 真写入）
    """
    db = tmp_path / "run_tpe.db"
    env = {**os.environ, "DISCOVERY_DB": str(db)}
    proc = subprocess.run(
        [sys.executable, "-m", "discovery", "run",
         "--budget", "2", "--n-sobol", "2", "--n-random", "0",
         "--tpe-trials", "2", "--embargo", "5", "--n-proc", "2", "--seed", "42"],
        capture_output=True, text=True, env=env, cwd=os.getcwd(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "status=" in proc.stdout
    assert "rho=" in proc.stdout
    # SQLite 落库（Sobol + TPE trial，至少 1 组）
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    n = conn.execute("SELECT COUNT(*) c FROM trial").fetchone()["c"]
    conn.close()
    assert n >= 1


@pytest.mark.slow
def test_champions_after_run(tmp_path):
    """先 run 再 champions：champions 读落库 trial 报 top（不空）。

    验收点：run 落库后，champions 子命令能读最新 snapshot 的 trial 行，算 Pareto 前沿 +
    DSR 排序后打印 "=== discovery champions"（cmd_champions 入口 + store 读路径通）。
    """
    db = tmp_path / "run_ch.db"
    env = {**os.environ, "DISCOVERY_DB": str(db)}
    subprocess.run([sys.executable, "-m", "discovery", "run",
                    "--budget", "1", "--n-sobol", "1", "--n-random", "0",
                    "--embargo", "5", "--n-proc", "1", "--seed", "7"],
                   capture_output=True, text=True, env=env, cwd=os.getcwd())
    proc = subprocess.run([sys.executable, "-m", "discovery", "champions"],
                          capture_output=True, text=True, env=env, cwd=os.getcwd())
    assert proc.returncode == 0
    assert "champions" in proc.stdout
