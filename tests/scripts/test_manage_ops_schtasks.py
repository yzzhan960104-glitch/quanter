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


def test_data_check_tasks_cover_t1_t2_and_daily_incremental():
    """DATA_CHECK_TASKS 含检查点①② + daily 增量（Phase 1.5 任务3）。

    核实点：
      ① 任务名集合含 QuanterDailyIncremental（@17:30 拉 daily）；
      ② 时序：daily @17:30 介于 T1 @17:00 / T2 @18:30 之间（拉新 → 检查 的链路顺序）；
      ③ bat 路径指向 scripts\\run_daily_incremental.bat。
    """
    by_name = {t: (time, bat) for t, time, bat in m.DATA_CHECK_TASKS}
    assert "QuanterDataCheckT1" in by_name
    assert "QuanterDataCheckT2" in by_name
    assert "QuanterDailyIncremental" in by_name
    # 时序断言：daily @17:30 早于 T2 @18:30（拉新先于检查的物理约束）
    t1 = by_name["QuanterDataCheckT1"][0]
    daily = by_name["QuanterDailyIncremental"][0]
    t2 = by_name["QuanterDataCheckT2"][0]
    assert t1 < daily < t2
    # bat 路径
    assert by_name["QuanterDailyIncremental"][1].endswith("run_daily_incremental.bat")
