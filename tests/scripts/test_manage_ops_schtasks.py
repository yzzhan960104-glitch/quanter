# -*- coding: utf-8 -*-
"""schtasks 管理脚本单测——只测任务定义/命令构造逻辑，不真跑 schtasks（系统级副作用）。

Why 不真跑：schtasks /Create 会写入 Windows 任务计划程序（系统级副作用），冒烟应放在
端到端阶段手工执行；单测层只验任务定义（RETIRED/LEGACY 清退清单）+ register() 命令构造
逻辑，保证退役任务不被重建、回归有红线拦截。

⚠️ C-2 scheduling-orchestration Task 9 收口（Final Fix）：
    2 个 supervisor（QuanterDataPipeline / QuanterBrief）已退役，职责收进 uvicorn 的
    ``pipeline_then_eod`` cron 事件链。``register()`` **绝不 /Create 它们**（否则采集跑两遍 /
    brief 推两份），只 /Delete 清退。``PIPELINE_TASKS`` 仅保留为历史元数据供 --list/--rerun。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops import manage_ops_schtasks as m


def test_retired_tasks_covers_pipeline_brief():
    """RETIRED_TASKS：至少覆盖 Task 9 收编的两个退役 schtasks（pipeline/brief）。

    Why 完整覆盖：这两个任务的职责已收进 uvicorn ``pipeline_then_eod`` 事件链，升级环境
    若残留旧 schtasks 会与新链重复触发（采集@17:00 schtask + 事件链采集）。RETIRED_TASKS
    必须至少列全这两个，register()/unregister()/unregister_pipeline_brief() 清退时都查这份。

    ⚠️ T8 / C-9 A3 调整：原断言用严格集合相等 ``==`` 锁死两个成员，但 C-8 后
    ``QuanterDailyBrief`` 也需加入 RETIRED_TASKS 幂等清退（已删系统任务，防残留/误重建）。
    严格相等会让本次补充失败，故改为子集断言 ``>=`` —— 锁住"两个原成员必在"的回归红线，
    同时允许后续补充（如 QuanterDailyBrief）而不破坏既有意图。新成员的完整性由
    tests/test_manage_ops_schtasks.py::test_retired_tasks_contains_daily_brief 锁定。
    """
    # 子集断言：Task 9 两个原成员必须在（回归红线），允许后续补充（如 QuanterDailyBrief）
    assert {"QuanterDataPipeline", "QuanterBrief"} <= set(m.RETIRED_TASKS)


def test_pipeline_tasks_kept_as_historical_metadata():
    """PIPELINE_TASKS：历史 supervisor 元数据（时间 + bat），register 不再 /Create。

    Final Fix：PIPELINE_TASKS 仅供 --list / --rerun 对历史已注册环境清查；register() 不再
    迭代它创建。本测试锁定历史定义（防误删元数据导致 --list/--rerun 空跑）。
    """
    by_name = {t: (time, bat) for t, time, bat in m.PIPELINE_TASKS}
    assert set(by_name) == {"QuanterDataPipeline", "QuanterBrief"}
    # 数据链早于播报（历史时序：拉新先于播报）
    assert by_name["QuanterDataPipeline"][0] < by_name["QuanterBrief"][0]
    assert by_name["QuanterDataPipeline"][1].endswith("run_data_pipeline.bat")
    assert by_name["QuanterBrief"][1].endswith("run_brief_all.bat")


def test_legacy_tasks_cover_old_six():
    """LEGACY_TASKS：旧 6 零散任务全集（register 时 /Delete 清退防 schtasks 残留）。

    Why 完整覆盖：方案C 前注册过 6 零散任务的环境升级后若不清退，旧任务会并存重复触发；
    LEGACY_TASKS 必须列全旧 6 个，register 幂等清退。
    """
    assert set(m.LEGACY_TASKS) == {
        "QuanterTradingBrief", "QuanterStrategyBrief", "QuanterDataBrief",
        "QuanterDataCheckT1", "QuanterDailyIncremental", "QuanterDataCheckT2"}


def test_register_clears_legacy_and_retired_creates_nothing(monkeypatch):
    """register()：只 /Delete 清退 LEGACY + RETIRED，绝不 /Create（Final Fix）。

    物理意图（Final Fix · C-2 Task 9）：两个 supervisor 已退役（职责收进 uvicorn 事件链），
    register() 重建它们会造成双重触发（采集@17:00 schtask + 事件链采集、brief 推两份）。
    故 register() 现在只清退、不创建任何 schtasks。

    monkeypatch _schtasks 拦截系统调用，验：
      - /Delete 数 = LEGACY(6) + RETIRED(2) = 8（清退全集防残留）
      - /Create 数 = 0（绝不重建退役任务）
    不真写 Windows 任务计划程序。
    """
    calls = []
    monkeypatch.setattr(m, "_schtasks", lambda args: calls.append(args) or 0)
    m.register()
    deletes = [c for c in calls if "/Delete" in c]
    creates = [c for c in calls if "/Create" in c]
    # 清退 LEGACY + RETIRED = 8 删
    assert len(deletes) == len(m.LEGACY_TASKS) + len(m.RETIRED_TASKS)
    # Final Fix 红线：绝不创建任何 schtasks（两个 supervisor 已退役）
    assert len(creates) == 0
    # 删到的退役任务名正好是 RETIRED_TASKS 全集
    deleted_names = {c[c.index("/TN") + 1] for c in deletes if c[1] == "/TN" or "/TN" in c}
    # /TN 后跟任务名（schtasks /Delete /TN <name> /F）→ 取 /TN 的下一个 token
    retired_deleted = {
        c[c.index("/TN") + 1] for c in deletes if "/TN" in c
        and c[c.index("/TN") + 1] in m.RETIRED_TASKS
    }
    assert retired_deleted == set(m.RETIRED_TASKS)


def test_unregister_pipeline_brief_clears_retired(monkeypatch):
    """unregister_pipeline_brief()：/Delete RETIRED_TASKS 全集（幂等，供 start_all 调用）。"""
    calls = []
    monkeypatch.setattr(m, "_schtasks", lambda args: calls.append(args) or 0)
    m.unregister_pipeline_brief()
    deletes = [c for c in calls if "/Delete" in c]
    deleted_names = {c[c.index("/TN") + 1] for c in deletes}
    assert deleted_names == set(m.RETIRED_TASKS)
