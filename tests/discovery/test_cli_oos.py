# -*- coding: utf-8 -*-
"""cli oos 集成测试：subprocess 跑 python -m discovery oos，验证 exit 0 + 报告含关键行。

物理意图：端到端串起 Task 1-5 全链（freeze+evaluate+judging+store）。slow 标记因
跑真 oos 命令（freeze 读全 data_lake + 全历史 scan_symbol，~3-4min），fast 回归跳过；
手动验收 / 合并前必跑一次（spec §10 验收 gate）。
"""
import os
import subprocess
import sys

import pytest


@pytest.mark.slow
def test_oos_command_produces_report(tmp_path, monkeypatch):
    """跑 discovery oos（~3min），exit 0，stdout 含 outer 2026 + 落库 trial_id。

    用 tmp_path 隔离 db，避免污染 logs/discovery_trials.db（DISCOVERY_DB 环境变量
    覆盖 cli 的 DEFAULT_DB_PATH，见 cli._db_path）。
    """
    db = tmp_path / "t.db"
    env = {"DISCOVERY_DB": str(db)}  # cli 读取环境变量覆盖 DEFAULT_DB_PATH（见 Step 3）
    proc = subprocess.run(
        [sys.executable, "-m", "discovery", "oos", "--embargo", "5"],
        capture_output=True, text=True, env={**os.environ, **env},
        cwd=os.getcwd(),   # repo root（test 从 repo root 跑）
    )
    assert proc.returncode == 0, proc.stderr
    assert "★outer 2026" in proc.stdout
    assert "trial_id=" in proc.stdout
    assert "snapshot:" in proc.stdout
