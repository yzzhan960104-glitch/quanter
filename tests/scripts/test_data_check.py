"""数据检查点：①查T-1告警 / ②查T重采熔断。"""
from unittest.mock import patch, MagicMock
from scripts.run_data_check import run_check


def test_checkpoint1_t1_pass_no_alert():
    """检查点①：T-1 齐全 → 返 OK，不熔断。"""
    from data.freshness import FreshnessResult
    with patch("scripts.run_data_check.check_freshness",
               return_value=FreshnessResult("daily", True, "2026-07-22", "2026-07-22", "PASS")):
        r = run_check("t1", keys=("daily",))
    assert r["ok"] is True
    assert r["melted"] is False  # 检查点①永不熔断（T-1 历史缺不影响 T+1）


def test_checkpoint2_t_fail_triggers_resync_until_pass():
    """检查点②：T 未到位 → 重采，重采后 PASS → 不熔断。"""
    from data.freshness import FreshnessResult
    fail = FreshnessResult("daily", False, "2026-07-22", "2026-07-23", "陈旧")
    ok = FreshnessResult("daily", True, "2026-07-23", "2026-07-23", "PASS")
    sync = MagicMock(return_value=(True, "ok"))
    with patch("scripts.run_data_check.check_freshness", side_effect=[fail, ok]), \
         patch("scripts.run_data_check.sync_one_key", sync), \
         patch("scripts.run_data_check._now", side_effect=["18:30", "18:45"]):
        r = run_check("t2", keys=("daily",), deadline_hour=20)
    assert r["ok"] is True
    assert r["melted"] is False
    assert sync.call_count == 1  # 重采一次后 PASS


def test_checkpoint2_t_fail_after_deadline_melts():
    """检查点②：超时仍 FAIL → 熔断（不交易不自欺）。"""
    from data.freshness import FreshnessResult
    fail = FreshnessResult("daily", False, "2026-07-22", "2026-07-23", "陈旧")
    with patch("scripts.run_data_check.check_freshness", return_value=fail), \
         patch("scripts.run_data_check.sync_one_key", return_value=(False, "积分不足")), \
         patch("scripts.run_data_check._now", return_value="20:30"):  # 已超 20:00
        r = run_check("t2", keys=("daily",), deadline_hour=20)
    assert r["ok"] is False
    assert r["melted"] is True
