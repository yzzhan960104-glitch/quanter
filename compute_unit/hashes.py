# -*- coding: utf-8 -*-
"""跨机一致性哈希原语:git_commit / engine_hash / parquet_sha256。

env_check(校验路径)与 task_export(导出路径)共用——DRY 单源,未来改算法只改一处。
不依赖 discovery(只 import hashlib/subprocess/pathlib/strategies),故无 import 链耦合。
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

# 项目根(compute_unit/ 的上级 = quanter/)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git_head_sha() -> str:
    """当前 HEAD commit sha(git rev-parse HEAD)。失败返空串。

    env_check 校验路径:返空串 → 和 task.git_commit(非空)不等 → _check_hashes 报漂移。
    task_export 导出路径:Win 总有 git,正常返真 sha;异常返空串则 task.git_commit="",
    Mac 校验时报漂移——链条自洽。
    """
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _file_sha256(path) -> str:
    """文件全内容 sha256(64 hex),64KB 块流式读(省内存,parquet 435MB)。不存在返空串。"""
    path = Path(path)
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _engine_hash() -> str:
    """回测内核指纹:backtest.py + method_v0.py 内容 sha256[:12]。

    与 discovery/runner.py:_engine_hash 同款算法。内核一动(engine_hash 变),Mac 与 Win 不可比。
    """
    from strategies.neckline import backtest, method_v0
    h = hashlib.sha256()
    for f in (backtest.__file__, method_v0.__file__):
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


def parquet_path() -> Path:
    """a_shares_daily.parquet 路径(与 discovery.snapshot.LAKE_PATH 同源)。"""
    return PROJECT_ROOT / "data_lake" / "a_shares_daily.parquet"
