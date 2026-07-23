"""数据检查点：①查T-1告警 / ②查T重采熔断。"""
from unittest.mock import patch, MagicMock
from scripts.run_data_check import run_check, _resync_key


def test_checkpoint1_t1_pass_no_alert():
    """检查点①：T-1 齐全 → 返 OK，不熔断。"""
    from data.freshness import FreshnessResult
    with patch("scripts.run_data_check.check_freshness",
               return_value=FreshnessResult("daily", True, "2026-07-22", "2026-07-22", "PASS")):
        r = run_check("t1", keys=("daily",))
    assert r["ok"] is True
    assert r["melted"] is False  # 检查点①永不熔断（T-1 历史缺不影响 T+1）


def test_checkpoint2_t_fail_triggers_resync_until_pass():
    """检查点②：T 未到位 → 重采，重采后 PASS → 不熔断。

    daily key 走 sync_daily_incremental（Phase 1.5 任务1 分流后契约）。
    """
    from data.freshness import FreshnessResult
    fail = FreshnessResult("daily", False, "2026-07-22", "2026-07-23", "陈旧")
    ok = FreshnessResult("daily", True, "2026-07-23", "2026-07-23", "PASS")
    sync = MagicMock(return_value="OK 最新日 2026-07-23")
    with patch("scripts.run_data_check.check_freshness", side_effect=[fail, ok]), \
         patch("scripts.run_data_check.sync_daily_incremental", sync), \
         patch("scripts.run_data_check._now", side_effect=["18:30", "18:45"]):
        r = run_check("t2", keys=("daily",), deadline_hour=20)
    assert r["ok"] is True
    assert r["melted"] is False
    assert sync.call_count == 1  # 重采一次后 PASS


def test_checkpoint2_t_fail_after_deadline_melts():
    """检查点②：超时仍 FAIL → 熔断（不交易不自欺）。

    daily key 走 sync_daily_incremental（Phase 1.5 任务1 分流后契约）。
    """
    from data.freshness import FreshnessResult
    fail = FreshnessResult("daily", False, "2026-07-22", "2026-07-23", "陈旧")
    with patch("scripts.run_data_check.check_freshness", return_value=fail), \
         patch("scripts.run_data_check.sync_daily_incremental",
               side_effect=RuntimeError("积分不足")), \
         patch("scripts.run_data_check._now", return_value="20:30"):  # 已超 20:00
        r = run_check("t2", keys=("daily",), deadline_hour=20)
    assert r["ok"] is False
    assert r["melted"] is True


def test_checkpoint2_multi_round_resync_then_pass():
    """检查点②：多轮重采验证 15min 节流 + 跨轮重采后 PASS。

    场景：第1轮重采后重检仍 fail → time.sleep(15*60) 节流 → 第2轮重采后重检 ok。
    核实点：
      ① time.sleep 被调一次且参数=900s（15min 节流，非忙轮询）；
      ② sync_daily_incremental 调两次（两轮各重采一次，daily 分流后契约）；
      ③ 最终 return ok=True（重采收敛后不熔断）。
    _now 调用序列（每轮 while 条件 1 次 + sleep 前 1 次，PASS 轮不 sleep）：
      ["18:30"(第1轮 while), "18:30"(第1轮 sleep 前), "18:45"(第2轮 while)]
    check_freshness side_effect：[初始 fail, 第1轮重检 fail, 第2轮重检 ok]。
    """
    from data.freshness import FreshnessResult
    fail = FreshnessResult("daily", False, "2026-07-22", "2026-07-23", "陈旧")
    ok = FreshnessResult("daily", True, "2026-07-23", "2026-07-23", "PASS")
    sync = MagicMock(return_value="OK 最新日 2026-07-23")
    with patch("scripts.run_data_check.check_freshness", side_effect=[fail, fail, ok]), \
         patch("scripts.run_data_check.sync_daily_incremental", sync), \
         patch("scripts.run_data_check.time.sleep", side_effect=lambda _: None) as mock_sleep, \
         patch("scripts.run_data_check._now",
               side_effect=["18:30", "18:30", "18:45"]):
        r = run_check("t2", keys=("daily",), deadline_hour=20)
    assert r["ok"] is True
    assert r["melted"] is False
    assert sync.call_count == 2  # 两轮各重采一次
    assert mock_sleep.call_count == 1  # 仅第1轮 sleep 节流（第2轮 PASS 直接 return）
    assert mock_sleep.call_args.args == (900,)  # 15*60=900s


# ============================================================================
# _resync_key 按数据集 key 分流（Phase 1.5 任务1 TDD）
#
# 背景（数据链路闭环缺口）：
#   原 _resync_key 一律调 sync_incremental.sync_one_key，但 sync_incremental 的
#   quick 批不含 "daily"（A股日线原无日频增量机制）→ daily 陈旧时重采形同空转，
#   检查点②必熔断 eod_plan（明明有新数据可用，却因 sync 不到被判「不交易不自欺」）。
#   Phase 1.5 新增 scripts.sync_daily_incremental.sync_daily_incremental（分页批量拉
#   raw daily + adj_factor 重建前复权）补 daily 日频缺口；_resync_key 按 key 分流：
#     - key == "daily" → 走 sync_daily_incremental（返 str → 包成 (True, msg)）
#     - 其他 key      → 原 sync_one_key 逻辑（registry 语义 key 走通用增量）
# 语义边界：sync_daily_incremental 返 str 而非 tuple（与 sync_one_key 不同），故
# 分流层负责把 str 包成 tuple 统一外层契约；异常包成 (False, str(e))。
# ============================================================================


def test_resync_key_daily_routes_to_sync_daily_incremental():
    """daily 重采走 sync_daily_incremental（不走 sync_one_key），返 str → 包成 (True, msg)。

    物理意图：daily 陈旧时若走 sync_one_key 会空转（quick 批不含 daily），
    必须分流到 daily 日频增量采集器才能真把当天 daily 落湖。
    """
    sdi = MagicMock(return_value="OK 最新日 2026-07-24（+5778 行）")
    sok = MagicMock(return_value=(False, "不应被调"))
    with patch("scripts.run_data_check.sync_daily_incremental", sdi), \
         patch("scripts.run_data_check.sync_one_key", sok):
        ok, msg = _resync_key("daily")
    assert ok is True
    assert "OK 最新日" in msg  # sync_daily_incremental 的 str 原样透传
    assert sdi.call_count == 1
    assert sok.call_count == 0  # daily 不走 sync_one_key


def test_resync_key_daily_exception_returns_false():
    """sync_daily_incremental 抛异常 → 包成 (False, str(e))，不向主流程泄异常。"""
    sdi = MagicMock(side_effect=RuntimeError("tushare 限频"))
    with patch("scripts.run_data_check.sync_daily_incremental", sdi):
        ok, msg = _resync_key("daily")
    assert ok is False
    assert "tushare 限频" in msg


def test_resync_key_non_daily_falls_through_to_sync_one_key():
    """非 daily key 走原 sync_one_key 逻辑（registry 通用增量未变）。"""
    sok = MagicMock(return_value=(True, "ok"))
    sdi = MagicMock(return_value="不应被调")
    with patch("scripts.run_data_check.sync_one_key", sok), \
         patch("scripts.run_data_check.sync_daily_incremental", sdi):
        ok, _ = _resync_key("moneyflow")  # moneyflow 是 quick 批常规 key
    assert ok is True
    assert sok.call_count == 1
    assert sdi.call_count == 0  # 非 daily 不分流到 daily 增量

