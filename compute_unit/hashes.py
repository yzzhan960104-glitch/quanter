# -*- coding: utf-8 -*-
"""跨机一致性哈希原语:git_commit / engine_hash / parquet_sha256。

env_check(校验路径)与 task_export(导出路径)共用——DRY 单源,未来改算法只改一处。
不依赖 discovery(只 import hashlib/subprocess/pathlib/strategies),故无 import 链耦合。

P1-3(2026-08-02):engine_hash 指纹从「backtest.py + method_v0.py」扩展到完整回测内核
(replay/models/strategy/execution/signal/objective)。compute_unit v2 支持 replay 模式后,
任何内核文件改动都必须触发跨机漂移检测,否则 Mac 与 Win 静默跑出不同结果。
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


ENGINE_FILES = (
    "strategies/neckline/backtest.py",
    # T18（2026-08-15 · T7 强制遗留收口）：price_levels.py 现承载回测内核的价位数学
    # （compute_price_levels/PRICE_LEVEL_DEFAULTS），是 backtest.simulate_exit 的传递依赖
    # （backtest.py:42 `from .price_levels import ...`）。不在清单 = 指纹盲区：未来改价位
    # 公式（止损基准/tp 倍数口径）engine_hash 不变，老 trial 不标 stale，跨机 discovery
    # 静默误判可比——回测⇄实盘等价性头号资产的守门人自己漏了门。加入即 hash 变化属
    # 预期重估（backtest.py 内容 P1 后早变过，基线本就该刷新）。
    "strategies/neckline/price_levels.py",
    "strategies/neckline/method_v0.py",
    "strategies/neckline/strategy.py",
    "strategies/neckline/execution.py",
    "strategies/neckline/signal.py",
    "backtest/replay.py",
    "backtest/models.py",
    "discovery/objective.py",
)


def _engine_hash() -> str:
    """回测内核指纹:ENGINE_FILES 逐文件内容 sha256[:12]（文件名入 hash 防改名漏检）。

    discovery/runner.py:_engine_hash 是同款算法(双份实现,test_hashes 断言相等)。
    内核一动(engine_hash 变),Mac 与 Win 不可比。
    """
    h = hashlib.sha256()
    for rel in ENGINE_FILES:
        h.update(rel.encode("utf-8"))
        with open(PROJECT_ROOT / rel, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


def parquet_path() -> Path:
    """a_shares_daily.parquet 路径(与 discovery.snapshot.LAKE_PATH 同源)。"""
    return PROJECT_ROOT / "data_lake" / "a_shares_daily.parquet"
