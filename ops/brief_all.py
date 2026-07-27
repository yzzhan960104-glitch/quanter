# -*- coding: utf-8 -*-
"""播报 supervisor（方案 C · 原 3 个 schtasks 合并）：Trading + Strategy + Data 串推。

物理意图：
    原 QuanterTradingBrief@15:30 / StrategyBrief@16:00 / DataBrief@17:00 三个独立 schtasks，
    且 DataBrief@17:00 在日频采集@17:30 **之前**跑——播的是采集前的旧数据健康度（顺序 bug）。
    合并成 data_pipeline 之后的 supervisor @18:00，DataBrief 反映采集后真实状态。

故障隔离：
    单个 bot 推送失败（dws 故障/robotCode 缺）不阻断其余 bot——各自独立 robotCode +
    幂等文件（logs/.last_<bot>_brief），互不干扰。

时序约束：
    须在 QuanterDataPipeline @17:00 之后跑（@18:00），否则 DataBrief 仍播采集前旧数据。
    注册见 ops/manage_ops_schtasks.py PIPELINE_TASKS。

用法（schtasks 触发）：
    scripts/run_brief_all.bat → .venv310/Scripts/python.exe ops/brief_all.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
PY = sys.executable

# 三个播报 bot 串行（各自独立 robotCode + 幂等文件，互不干扰）
BOTS = ["trading", "strategy", "data"]


def main() -> int:
    print(f"=== brief_all 启动（{len(BOTS)} 个 bot 串行）===")
    rcs = []
    for bot in BOTS:
        print(f"\n--- {bot} 播报 ---")
        rc = subprocess.call([PY, "-m", "broadcast", "--bot", bot], cwd=str(ROOT))
        rcs.append((bot, rc))
        if rc != 0:
            print(f"⚠️ {bot} 播报失败 rc={rc}（继续其余 bot）")
    print("\n=== brief_all 汇总 ===")
    for bot, rc in rcs:
        print(f"  {'✅' if rc == 0 else '❌'} {bot}: rc={rc}")
    return 1 if any(rc != 0 for _, rc in rcs) else 0


if __name__ == "__main__":
    sys.exit(main())
