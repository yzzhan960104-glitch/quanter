# -*- coding: utf-8 -*-
"""APScheduler 工作日 cron 语义回归测试（2026-08-03 周一断链实证）。

背景：APScheduler 3.x 的 CronTrigger ``day_of_week`` 是 **0=周一**（非标准 cron 的
0=周日）。项目里 pipeline/pre_open/post_close/digest 曾用 ``"1-5"`` 表达"周一至周五"，
实际匹配**周二至周六**——2026-08-03（周一）18:00 pipeline 未触发即此 bug 实证
（quanter.log 无 Running job、data_pipeline.log 无今日采集）。本测试钉死语义，
防任何人改回 ``"1-5"``。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

_TZ = ZoneInfo("Asia/Shanghai")
_MONDAY_1700 = datetime(2026, 8, 3, 17, 0, tzinfo=_TZ)   # 2026-08-03 是周一


def test_apscheduler_day_of_week_zero_is_monday():
    """钉死 APScheduler 语义：0=周一，故 ``1-5`` 从周一起算下次触发是周二。"""
    t = CronTrigger.from_crontab("0 18 * * 1-5", timezone=_TZ)
    assert t.get_next_fire_time(None, _MONDAY_1700) == datetime(2026, 8, 4, 18, 0, tzinfo=_TZ)


def test_mon_fri_matches_monday():
    """``mon-fri`` 从周一起算 → 当天 18:00 触发（正确的工作日语义）。"""
    t = CronTrigger.from_crontab("0 18 * * mon-fri", timezone=_TZ)
    assert t.get_next_fire_time(None, _MONDAY_1700) == datetime(2026, 8, 3, 18, 0, tzinfo=_TZ)


def test_engine_cron_defaults_use_mon_fri():
    """engine 三个盘后 job 的默认 cron 必须用 ``mon-fri``（防 ``1-5`` 回归）。"""
    from trading.engine import (PIPELINE_CRON_DEFAULT, PRE_OPEN_CRON_DEFAULT,
                                POST_CLOSE_CRON_DEFAULT)
    for expr in (PIPELINE_CRON_DEFAULT, PRE_OPEN_CRON_DEFAULT, POST_CLOSE_CRON_DEFAULT):
        assert "mon-fri" in expr
        assert "1-5" not in expr


def test_digest_cron_default_uses_mon_fri():
    """research digest cron 默认值必须 ``mon-fri``（周一也推送，与 pipeline 同语义）。"""
    import presentation.server.main as main
    assert "mon-fri" in main._DIGEST_CRON_DEFAULT
    assert "1-5" not in main._DIGEST_CRON_DEFAULT
