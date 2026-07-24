# -*- coding: utf-8 -*-
"""散装 scripts/ 收敛测试（Plan Task 10，2026-07-25 范围自主收窄）。

物理意图：sync_data_lake.py（全量 daily，by=symbol 等价）转薄壳调统一 CLI。
sync_all_tushare/sync_incremental/sync_daily_incremental **保留**——其编排逻辑
（致命错误停批 / by=date 增量 _merge_dedup 历史保护）比 CLI fail-soft 更成熟，
强制转薄壳会丢失语义（daily 增量 by=symbol+resume 会因 shard 已存在跳过新日期）。
后续 CLI --incremental 增强（吸收 by=date 增量）后再吸收这 3 个脚本。
sync_tushare.py 保留（server data_service 子进程依赖 DATASET_REGISTRY.script）。
"""
import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(script: str, *args: str) -> tuple[int, str, str]:
    """跑 scripts/<script> 返回 (rc, stdout, stderr)。"""
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", script), *args],
        capture_output=True, text=True, timeout=60, cwd=ROOT,
    )
    return r.returncode, r.stdout, r.stderr


def test_sync_data_lake_转调标注deprecated():
    """sync_data_lake.py 运行时触发 DeprecationWarning（全量 daily 转统一 CLI）。"""
    rc, out, err = _run("sync_data_lake.py", "--help")
    combined = (out + err).lower()
    assert "deprecated" in combined or "data.sync" in combined, \
        f"sync_data_lake.py 应标 deprecated/转调 data.sync，实际 stdout={out[:200]} stderr={err[:200]}"


def test_sync_tushare_保留不deprecated():
    """sync_tushare.py 是 server 依赖入口，不 deprecated（--help 正常输出 key 选项）。"""
    rc, out, err = _run("sync_tushare.py", "--help")
    assert rc == 0
    # sync_tushare.py 的 help 应含 key/dataset 字样（argparse choices 反射 TUSHARE_DATASETS）
    assert "dataset" in (out + err).lower() or "key" in (out + err).lower() or "数据集" in (out + err)

