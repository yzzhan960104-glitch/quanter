# -*- coding: utf-8 -*-
"""交易计划否决工具（全自动模式的刹车 · pre_open 前手动调）。

物理意图：
    AUTO_CONFIRM_PLAN=true 全自动模式下，eod_plan 落盘即 confirmed=True，pre_open 次日
    直挂。研究员若对某单/全部有异议，必须在 pre_open（09:22）前调本工具否决——这是
    全自动模式下唯一的人审刹车（opt-out 红线：默认挂，否决才拦）。

    - 不传 symbol → confirmed=False（pre_open 跳过整个 plan，全不挂）
    - 传 symbol  → 从 orders 删该 symbol（pre_open 挂其余，仅拦这一单）

用法：
    .venv310/Scripts/python.exe trading/tools/veto_plan.py              # 否决今日全部
    .venv310/Scripts/python.exe trading/tools/veto_plan.py 2026-07-28   # 否决指定日全部
    .venv310/Scripts/python.exe trading/tools/veto_plan.py 2026-07-28 300654.SZ  # 仅否决该票

幂等：重复否决不报错（已 confirmed=False / 已无该 symbol → 提示后返 0）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

# 三层 dirname：本脚本 → tools → trading → 项目根（与 trigger_eod_once.py 同范式）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_PROJECT_ROOT)  # TRADE_PLAN_DIR 默认 logs/trading_plans 相对路径，须 cwd=项目根
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from trading import trading_plan
# C-6 V3：单一时间源（CLI 今日默认走 clock.today，与 engine.py V2 同款）。
from trading import clock


def veto(date: str, symbol: str | None = None) -> int:
    """否决某日计划：symbol=None 全否（confirmed=False），传 symbol 单否（删该 order）。

    Returns:
        0=否决成功（含幂等已否）；1=无计划/未找到 symbol。
    """
    plan = trading_plan.load_plan(date)
    if plan is None:
        print(f"[veto] 无计划 {date}（未落盘或损坏），无需否决")
        return 1

    if symbol is None:
        # 全否：confirmed=False（pre_open 跳过整个 plan，orders 保留供复盘）
        if plan.get("confirmed") is False:
            print(f"[veto] {date} 已是 confirmed=False（幂等，无需重复否决）")
            return 0
        plan["confirmed"] = False
        trading_plan._plan_path(date).write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[veto] 已否决 {date} 全部（confirmed=False，pre_open 将跳过整个计划）")
        return 0

    # 单否：从 orders 删该 symbol（pre_open 挂其余）
    before = len(plan.get("orders", []))
    plan["orders"] = [o for o in plan.get("orders", [])
                      if o.get("order", {}).get("symbol") != symbol]
    after = len(plan["orders"])
    if before == after:
        print(f"[veto] {date} 计划无 {symbol}（未找到，可能已否决）")
        return 0  # 幂等：已无该 symbol 视作成功
    trading_plan._plan_path(date).write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[veto] 已否决 {date} {symbol}（{before}→{after} 单，pre_open 挂其余）")
    return 0


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else clock.today()
    symbol = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(veto(date, symbol))
