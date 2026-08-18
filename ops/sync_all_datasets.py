# -*- coding: utf-8 -*-
"""每日全数据集尝试同步（2026-08-19 · 用户裁决「所有数据每日都尝试更新到最新值」）。

物理背景：18:00 pipeline 的 STEPS 只采 daily（策略唯一声明消费集，精益设计）；其余
40+ 数据集注册在 DATASET_REGISTRY（含 schedule 字段）但**从无调度器消费**——只能
前端手动触发，面板因此出现 lag 68h/181h 的停滞与残留 syncing 哨兵。本脚本把
「每日尝试」落地：遍历全部带 script 的数据集逐集增量同步（sync_tushare 管道
resume=True，shard 已最新即跳过——幂等且配额友好，无新数据 no-op）。

挂接：pipeline_then_eod 尾部（brief 后）异步 spawn（DETACHED fire-and-forget），
绝不阻塞 eod 主链——贵数据集（cyq_perf/share_float 等 by=symbol 全市场）夜间慢跑。

哨兵协议（与 presentation/server/services/data_service.py 前端同步通道完全兼容，
同目录 data_lake/.syncing/ 同命名 {key} / {key}.failed）：
  - 启动时清全部 .syncing 孤儿哨兵（每日重试语义下，启动前不可能有合法进行中的
    同步；残留必属昨日进程死亡遗留——顺带治面板「syncing(lag 181h)」的误导显示）；
  - .failed 保留（供面板展示上次失败原因），当日重试成功时自然清除。

限频：串行逐集（不并发轰炸 Tushare）+ 管道内建限频/熔断（tushare_sync 统一管道）；
单集超时 30 分钟（比前端通道的 10 分钟宽——夜间无人等待）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 直跑防御：python ops/sync_all_datasets.py 时 sys.path[0]=ops/，项目包不可见——
# 注入根目录（挂接方走 -m 形式，与 data_pipeline STEPS 同惯例）。
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SYNCING_DIR = ROOT / "data_lake" / ".syncing"
LOG = ROOT / "logs" / "sync_all_datasets.log"

# daily 已由 pipeline STEPS ② 采集（同日二跑纯浪费 ~2 分钟全市场拉取），排除。
SKIP_KEYS = {"daily"}

# 单集超时（秒）：by=symbol 全市场贵数据集夜间跑给宽限；超时 kill 记 .failed。
PER_DATASET_TIMEOUT_SEC = 30 * 60


def _sentinel_path(key: str, failed: bool = False) -> Path:
    return SYNCING_DIR / (f"{key}.failed" if failed else key)


def _clear_sentinel(key: str) -> None:
    for p in (_sentinel_path(key), _sentinel_path(key, failed=True)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _mark_failed(key: str, message: str) -> None:
    """写 .failed（先清 syncing，状态机单值——协议同 data_service._mark_failed）。"""
    _sentinel_path(key).unlink(missing_ok=True)
    try:
        SYNCING_DIR.mkdir(parents=True, exist_ok=True)
        _sentinel_path(key, failed=True).write_text(
            message[-2000:], encoding="utf-8")
    except OSError:
        pass


def _build_cmd(key: str, spec: dict) -> list[str]:
    """构造单集同步命令（C-9 A1 同款：sync_tushare.py 需注入位置参数 key）。"""
    script_abs = str(ROOT / spec["script"])
    cmd = [sys.executable, script_abs, *list(spec.get("args", []))]
    if os.path.basename(spec["script"]) == "sync_tushare.py":
        cmd.insert(2, key)
    return cmd


def sweep_orphan_sentinels() -> int:
    """清孤儿 syncing 哨兵，返清除数。

    精确匹配：只删「与 DATASET_REGISTRY key 同名且无后缀」的文件——.syncing 目录
    被历史脚本复用为临时区（实测混有 parquet/log 等），无差别清会误删；
    .failed 保留（面板上次失败原因）。
    """
    from config import DATASET_REGISTRY
    keys = {f"{k}" for k in DATASET_REGISTRY}
    n = 0
    if SYNCING_DIR.exists():
        for p in SYNCING_DIR.iterdir():
            if p.is_file() and p.name in keys:
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
    return n


def run_all(timeout_sec: int = PER_DATASET_TIMEOUT_SEC) -> tuple[int, int]:
    """串行同步全部数据集，返 (成功数, 失败数)。哨兵按结果写/清。"""
    from config import DATASET_REGISTRY
    ok = fail = 0
    for key, spec in sorted(DATASET_REGISTRY.items()):
        if key in SKIP_KEYS or not spec.get("script"):
            continue
        SYNCING_DIR.mkdir(parents=True, exist_ok=True)
        _sentinel_path(key).write_text(
            f"auto {time.strftime('%F %T')}", encoding="utf-8")
        try:
            proc = subprocess.run(
                _build_cmd(key, spec), capture_output=True, text=True,
                timeout=timeout_sec, check=False, cwd=str(ROOT))
        except subprocess.TimeoutExpired:
            _mark_failed(key, f"超时（>{timeout_sec}s）被 kill")
            fail += 1
            print(f"❌ {key}: 超时", flush=True)
            continue
        if proc.returncode == 0:
            _clear_sentinel(key)
            ok += 1
            print(f"✅ {key}", flush=True)
        else:
            tail = (proc.stderr or proc.stdout or f"退出码 {proc.returncode}")
            _mark_failed(key, tail.strip()[-2000:])
            fail += 1
            print(f"❌ {key}: rc={proc.returncode} {tail.strip()[-200:]}", flush=True)
    return ok, fail


def main() -> int:
    from infra.pyio import force_utf8_stdout
    force_utf8_stdout()
    n_orphan = sweep_orphan_sentinels()
    print(f"=== sync_all_datasets 启动（清孤儿哨兵 {n_orphan} 个）===", flush=True)
    t0 = time.monotonic()
    ok, fail = run_all()
    print(f"=== 汇总：✅{ok} ❌{fail}（{time.monotonic()-t0:.0f}s）===", flush=True)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
