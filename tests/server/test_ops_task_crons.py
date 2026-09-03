# -*- coding: utf-8 -*-
"""W9 六运维 schtasks 收编 server 的注册面测试（2026-08-30）。

钉死三件：
  ① OPS_TASK_CRONS 表完整性：W9 六项 + 2026-09-01 计划预演槽（18:10）与
     公网发布槽（9-15 时每小时）+ 2026-09-03 亏损归因（18:05）/探索环（18:45）/
     换代 watch（18:50）三槽共 11 项、job_id 唯一、W9 六项与原 schtasks 时刻表逐字对齐（晨检 09:40/
     台账 15:40/EOD 15:45/对照 15:50/audit 16:05/guard 5min）；
  ② register_ops_task_crons 用 fake scheduler 走全表（独立函数免起 app）；
  ③ manage_ops_schtasks.RETIRED_TASKS 含六名（双轨退役防重建的清退面）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _main():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "server_main_for_crons", ROOT / "presentation" / "server" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def main_mod():
    return _main()


def test_table_has_eleven_unique_jobs_matching_schedule(main_mod):
    table = main_mod.OPS_TASK_CRONS
    assert len(table) == 11
    ids = [t[0] for t in table]
    assert len(set(ids)) == 11
    sched = {t[0]: (t[2], t[3]) for t in table}
    assert sched["ops_morning_check"] == ("cron", {"hour": 9, "minute": 40})
    assert sched["ops_emquant_ingest"] == ("cron", {"hour": 15, "minute": 40})
    assert sched["ops_eod_report"] == ("cron", {"hour": 15, "minute": 45})
    assert sched["ops_gm_ab_compare"] == ("cron", {"hour": 15, "minute": 50})
    assert sched["ops_audit_ssot"] == ("cron", {"hour": 16, "minute": 5})
    assert sched["ops_gm_guard"] == ("interval", {"seconds": 300})
    # 2026-09-01 计划→播报桥第三块：管道 18:00-18:02 落完 T 日湖数据后的预演槽
    assert sched["ops_plan_preview"] == (
        "cron", {"hour": 18, "minute": 10, "day_of_week": "mon-fri"})
    # 2026-09-01 公网发布（yzzhan.xin）：交易日 9:35~15:35 每小时（CF 免费额度内）
    assert sched["ops_publish_public"] == (
        "cron", {"hour": "9-15", "minute": 35, "day_of_week": "mon-fri"})
    # 2026-09-03 亏损归因：用户裁决 18 点段；18:05 避 18:00-18:02 管道写湖窗口
    assert sched["ops_loser_review"] == (
        "cron", {"hour": 18, "minute": 5, "day_of_week": "mon-fri"})
    # 2026-09-03 探索环：归因/digest 之后，止步 DRAFT 红线
    assert sched["ops_explore_loop"] == (
        "cron", {"hour": 18, "minute": 45, "day_of_week": "mon-fri"})
    # 换代 watch：分叉检测+就绪包；deploy 人审触发永不进 cron
    assert sched["ops_promote_watch"] == (
        "cron", {"hour": 18, "minute": 50, "day_of_week": "mon-fri"})


def test_register_with_fake_scheduler_arms_all(main_mod):
    armed_calls = []

    class FakeSched:
        def add_job(self, fn, trigger, *, id, replace_existing, **kw):
            armed_calls.append((id, trigger, kw))

    armed = main_mod.register_ops_task_crons(FakeSched())
    assert armed == [t[0] for t in main_mod.OPS_TASK_CRONS]
    assert len(armed_calls) == 11
    guard = [c for c in armed_calls if c[0] == "ops_gm_guard"][0]
    assert guard[1] == "interval" and guard[2] == {"seconds": 300}


def test_register_soft_degrades_on_single_failure(main_mod):
    class FakeSched2:
        calls = 0

        def add_job(self, fn, trigger, *, id, replace_existing, **kw):
            FakeSched2.calls += 1
            if FakeSched2.calls == 1:
                raise RuntimeError("boom")

    armed = main_mod.register_ops_task_crons(FakeSched2())
    assert len(armed) == 10         # 首项失败软降级，其余十项照挂


def test_retired_tasks_cover_six_for_cleanup():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "mos_for_test", ROOT / "ops" / "manage_ops_schtasks.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    six = {"QuanterEmquantMorningCheck", "QuanterEmquantIngest",
           "QuanterEmquantEodReport", "QuanterGmAbCompare",
           "QuanterAudit", "QuanterGmGuard"}
    assert six.issubset(set(mod.RETIRED_TASKS))
