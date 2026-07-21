# -*- coding: utf-8 -*-
"""schtasks 管理脚本单测（Task 6）——只测命令生成逻辑，不真跑 schtasks。

Why 不真跑：schtasks /Create 会写入 Windows 任务计划程序（系统级副作用），
冒烟应放在 Task 13 端到端阶段手工执行；单测层只验证命令构造逻辑（读 .env、
任务名映射、bat 路径），保证改时间/改任务名等回归有红线拦截。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import manage_ops_schtasks as m


def test_register_command_builder(monkeypatch):
    """命令生成含任务名/时间/bat 路径，且读 .env 的 BRIEF_TIME。"""
    monkeypatch.setenv("TRADING_BRIEF_TIME", "15:30")
    monkeypatch.setenv("STRATEGY_BRIEF_TIME", "16:00")
    monkeypatch.setenv("DATA_BRIEF_TIME", "17:00")
    cmds = m.build_register_commands()
    by_name = {c["task"]: c for c in cmds}
    assert "QuanterTradingBrief" in by_name
    assert by_name["QuanterTradingBrief"]["time"] == "15:30"
    assert by_name["QuanterTradingBrief"]["bat"].endswith("run_trading_brief.bat")
    assert by_name["QuanterStrategyBrief"]["time"] == "16:00"


def test_task_names_complete():
    names = m.TASK_NAMES
    assert set(names.values()) == {"QuanterTradingBrief", "QuanterStrategyBrief", "QuanterDataBrief"}
