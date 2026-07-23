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


def test_checkpoint2_multi_round_resync_then_pass():
    """检查点②：多轮重采验证 15min 节流 + 跨轮重采后 PASS。

    场景：第1轮重采后重检仍 fail → time.sleep(15*60) 节流 → 第2轮重采后重检 ok。
    核实点：
      ① time.sleep 被调一次且参数=900s（15min 节流，非忙轮询）；
      ② sync_one_key 调两次（两轮各重采一次）；
      ③ 最终 return ok=True（重采收敛后不熔断）。
    _now 调用序列（每轮 while 条件 1 次 + sleep 前 1 次，PASS 轮不 sleep）：
      ["18:30"(第1轮 while), "18:30"(第1轮 sleep 前), "18:45"(第2轮 while)]
    check_freshness side_effect：[初始 fail, 第1轮重检 fail, 第2轮重检 ok]。
    """
    from data.freshness import FreshnessResult
    fail = FreshnessResult("daily", False, "2026-07-22", "2026-07-23", "陈旧")
    ok = FreshnessResult("daily", True, "2026-07-23", "2026-07-23", "PASS")
    sleep = MagicMock()
    sync = MagicMock(return_value=(True, "ok"))
    with patch("scripts.run_data_check.check_freshness", side_effect=[fail, fail, ok]), \
         patch("scripts.run_data_check.sync_one_key", sync), \
         patch("scripts.run_data_check.time.sleep", side_effect=lambda _: None) as mock_sleep, \
         patch("scripts.run_data_check._now",
               side_effect=["18:30", "18:30", "18:45"]):
        r = run_check("t2", keys=("daily",), deadline_hour=20)
    assert r["ok"] is True
    assert r["melted"] is False
    assert sync.call_count == 2  # 两轮各重采一次
    assert mock_sleep.call_count == 1  # 仅第1轮 sleep 节流（第2轮 PASS 直接 return）
    assert mock_sleep.call_args.args == (900,)  # 15*60=900s
