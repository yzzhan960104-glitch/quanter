# -*- coding: utf-8 -*-
"""补发周日(2026-08-30)漏掉的两条非交易日心跳——按 ops 代码原样文案重放。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops.gm_ops_common import notify

notify("INFO", "掘金晨检 2026-08-30 非交易日（周末），跳过深检——schtask 心跳正常")
notify("INFO", "掘金EOD 2026-08-30 非交易日（周末），跳过日终播报——schtask 心跳正常")
print("[done] 两条周日心跳已补发")
