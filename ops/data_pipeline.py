# -*- coding: utf-8 -*-
"""数据链 supervisor（方案 C · 原 3 个 schtasks 合并）：T1 检查 → 日频采集 → T2 检查。

物理意图：
    原 QuanterDataCheckT1@17:00 / DailyIncremental@17:30 / DataCheckT2@18:30 三个独立
    schtasks，靠时间差保证「采集 → T2 检查」顺序——脆弱（schtasks 漂移/采集超时 → T2
    读旧数据）。合并成一个 supervisor 串行跑，时序由代码保证，且 schtasks 数 -2。

时序（串行，每步等完才下一步）：
    ① T1 检查（查 T-1 完整性，告警级）
    ② 日频采集（sync_daily_incremental 拉 T 日 daily）
    ③ T2 检查（查 T 完整性，FAIL 重采→熔断 eod）

故障隔离（Grill Me）：
    - T1 失败（告警）不阻断采集/T2（T1 是 T-1 健康度，与 T 采集无关）
    - 采集失败不阻断 T2（T2 本就是检测采集结果的，会发现 T 数据缺失→重采/熔断兜底）
    - T2 是最后一步，自身熔断逻辑不变（FAIL → eod 不扫信号）

用法（schtasks 触发）：
    scripts/run_data_pipeline.bat → .venv310/Scripts/python.exe ops/data_pipeline.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)  # 锁项目根（子任务用相对路径 data/tools/...）
PY = sys.executable  # .bat 用 .venv310 跑本脚本，子任务复用同一解释器

# (步骤名, 命令) —— 串行顺序即物理时序
STEPS = [
    ("① T1 检查（T-1 完整性）",   [PY, "-m", "data.tools.run_data_check", "t1"]),
    ("② 日频采集（T 日 daily）",  [PY, "data/tools/sync_daily_incremental.py"]),
    ("③ T2 检查（T 完整性·熔断）", [PY, "-m", "data.tools.run_data_check", "t2"]),
]


def main() -> int:
    print(f"=== data_pipeline 启动（{len(STEPS)} 步串行）===")
    rcs = []
    for name, cmd in STEPS:
        print(f"\n--- {name} ---")
        rc = subprocess.call(cmd, cwd=str(ROOT))
        rcs.append((name, rc))
        if rc != 0:
            # 故障隔离：T1/采集失败不阻断后续（T2 兜底检测采集结果）。只记录，不 raise。
            print(f"⚠️ {name} 失败 rc={rc}（继续后续步骤）")
    print("\n=== data_pipeline 汇总 ===")
    for name, rc in rcs:
        print(f"  {'✅' if rc == 0 else '❌'} {name}: rc={rc}")
    # 任一步失败 → supervisor 返非 0（便于 schtasks 监测/日志，但不阻断次日调度）
    return 1 if any(rc != 0 for _, rc in rcs) else 0


if __name__ == "__main__":
    sys.exit(main())
