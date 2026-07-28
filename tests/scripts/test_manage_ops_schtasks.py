# -*- coding: utf-8 -*-
"""schtasks 管理脚本单测——只测任务定义/命令构造逻辑，不真跑 schtasks（系统级副作用）。

Why 不真跑：schtasks /Create 会写入 Windows 任务计划程序（系统级副作用），冒烟应放在
端到端阶段手工执行；单测层只验任务定义（PIPELINE_TASKS supervisor + LEGACY_TASKS 清退）
+ register() 命令构造逻辑，保证改时间/改任务名等回归有红线拦截。

方案C 重构（memory schtasks方案C合并 7→2）：原 6 零散任务（TradingBrief/StrategyBrief/
DataBrief/DataCheckT1/T2/DailyIncremental）合并为 2 supervisor（DataPipeline 跑数据链、
Brief 跑播报），各 supervisor 内部 bat 串联子任务。本测试对齐新架构（旧 build_register_
commands/TASK_NAMES/DATA_CHECK_TASKS API 已废弃）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops import manage_ops_schtasks as m


def test_pipeline_tasks_two_supervisors():
    """PIPELINE_TASKS：2 supervisor（数据链 17:00 + 播报 18:00），bat 路径 + 时序对。

    物理意图：数据链 supervisor 先跑（17:00 拉增量 + 检查点），播报 supervisor 后跑
    （18:00 读已落湖数据出播报）——拉新先于播报的物理时序在任务时间上硬约束。
    """
    by_name = {t: (time, bat) for t, time, bat in m.PIPELINE_TASKS}
    assert set(by_name) == {"QuanterDataPipeline", "QuanterBrief"}
    # 数据链早于播报（拉新先于播报的物理时序）
    assert by_name["QuanterDataPipeline"][0] < by_name["QuanterBrief"][0]
    assert by_name["QuanterDataPipeline"][1].endswith("run_data_pipeline.bat")
    assert by_name["QuanterBrief"][1].endswith("run_brief_all.bat")


def test_legacy_tasks_cover_old_six():
    """LEGACY_TASKS：旧 6 零散任务全集（register 时 /Delete 清退防 schtasks 残留）。

    Why 完整覆盖：方案C 前注册过 6 零散任务的环境升级后若不清退，旧任务会与新 supervisor
    并存重复触发；LEGACY_TASKS 必须列全旧 6 个，register 幂等清退。
    """
    assert set(m.LEGACY_TASKS) == {
        "QuanterTradingBrief", "QuanterStrategyBrief", "QuanterDataBrief",
        "QuanterDataCheckT1", "QuanterDailyIncremental", "QuanterDataCheckT2"}


def test_register_clears_legacy_then_creates_pipeline(monkeypatch):
    """register()：先 /Delete 清退 LEGACY_TASKS，再 /Create 创建 PIPELINE_TASKS（幂等）。

    monkeypatch _schtasks 拦截系统调用，验命令构造（清退数=LEGACY 数，创建数=PIPELINE 数），
    不真写 Windows 任务计划程序。
    """
    calls = []
    monkeypatch.setattr(m, "_schtasks", lambda args: calls.append(args) or 0)
    m.register()
    deletes = [c for c in calls if "/Delete" in c]
    creates = [c for c in calls if "/Create" in c]
    # 清退 6 LEGACY + 2 PIPELINE 先删（幂等：创建前先删防 schtasks 已存在报错）= 8 删
    assert len(deletes) == len(m.LEGACY_TASKS) + len(m.PIPELINE_TASKS)
    assert len(creates) == len(m.PIPELINE_TASKS)  # 创建 2 新 supervisor
