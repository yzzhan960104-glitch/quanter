"""数据实时性检查点入口（schtasks 调度）。

两个检查点（brainstorm 决策 A 双检查点）：
  ① @17:00 查 T-1：历史数据应齐全，FAIL 仅告警（不影响 T+1 计划的 T 日数据）。
  ② @18:30 查 T：T 日数据是 T+1 计划输入，FAIL → 重采窗口（每15min重采至 deadline），
                 仍 FAIL → 熔断 eod_plan（不交易不自欺，绝不用 T-1 兜底算 T+1＝前视偏差）。

退出码：0=PASS/告警；2=熔断（eod_plan 应据此跳过，schtasks 层另处理）。
"""
from __future__ import annotations
import sys
import io
import time
import logging
from datetime import datetime

from data.freshness import check_freshness
from trading.calendar import expected_latest_trade_day
# 模块级 import（非 _resync_key 内局部 import）：测试需 patch "scripts.run_data_check.sync_one_key"
# 才能隔离真实采集，故必须把名字绑到模块命名空间。sync_incremental 模块本身 import 轻量
# （tushare 是其内部延迟 import），不会拖慢本入口启动。
from scripts.sync_incremental import sync_one_key
# daily 日频增量采集器（Phase 1.5 数据链路闭环）：
# 模块级 import 同样为测试可 patch "scripts.run_data_check.sync_daily_incremental" 隔离真实采集。
# Why 单独 import 而非懒加载：sync_daily_incremental 内部延迟 import pandas/tushare，
# 本入口启动时仅 import 该函数对象，不触发重依赖加载，与 sync_one_key 等价轻量。
from scripts.sync_daily_incremental import sync_daily_incremental

logger = logging.getLogger(__name__)


def _now() -> str:
    """当前 HH:MM（测试可 patch）。物理意图：判断是否超重采截止时间。"""
    return datetime.now().strftime("%H:%M")


def _resync_key(key: str) -> tuple[bool, str]:
    """重采单个数据集（按 key 分流）。

    ⚠️ key 是 registry 语义 key（如 "daily"），不是 parquet 文件名（如 "a_shares_daily"）。

    分流语义（Phase 1.5 数据链路闭环修复）：
      - key == "daily"：A股日线原无日频增量机制（sync_incremental quick 批不含 daily），
        走 sync_daily_incremental（分页批量 raw daily + adj_factor 重建前复权）。返 str
        → 包成 (True, msg) 统一外层契约；异常包成 (False, str(e)) 不向主流程泄。
      - 其他 key：走原 sync_one_key 逻辑（registry 通用增量 quick/slow 批）。

    Why 不在 sync_incremental 加 daily：daily 全市场 ~5500 标的，分页批量（limit=500
    绕过 ConnectionReset）+ adj_factor 重建前复权是独立物理路径，与 sync_incremental
    按 key 轮询的模式不兼容；独立脚本 + 分流是最直白的边界（守 Layer2 §7 单一职责）。
    """
    if key == "daily":
        # daily 日频增量：sync_daily_incremental 返 str（含新交易日/除权标注），
        # 分流层负责包成 tuple 统一外层契约，异常吞掉返 (False, msg)。
        try:
            msg = sync_daily_incremental()
            return True, msg
        except Exception as e:
            return False, str(e)
    # 非 daily：原 sync_one_key 逻辑（fallback_years=3 / max_days=None 与 brainstorm 钉死一致）
    today_str = datetime.today().strftime("%Y-%m-%d")
    return sync_one_key(key, today_str, fallback_years=3, max_days=None, log=io.StringIO())


def _alert(msg: str, level: str = "WARN") -> None:
    """钉钉告警（fire_and_forget，失败软降级）。

    ⚠️ import 走 ``infra.notifier`` 真身（与 Task10 engine.py handler 同口径）；
       ``core.notifier`` 是 strangler 转发垫片，未来下线后会隐性断链，故直指 infra 真身。
    """
    try:
        from infra.notifier import NotificationManager, fire_and_forget
        fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, level))
    except Exception:
        logger.exception("告警发送失败（不影响检查主流程）")


def run_check(
    checkpoint: str,
    *,
    keys: tuple[str, ...] = ("daily",),
    deadline_hour: int = 20,
) -> dict:
    """执行一个数据检查点。

    Args:
        checkpoint: "t1"（查T-1，告警不熔断）/ "t2"（查T，重采熔断）。
        keys:       检查的数据集 registry key 列表。
        deadline_hour: t2 重采截止小时（超过即熔断，默认 20 点）。

    Returns:
        {"ok":bool, "melted":bool, "details":[...]}
    """
    now = datetime.now()
    # t1=盘前期望T-1；t2=盘后期望T（expected_latest_trade_day 据 now.time 自动判定）
    expected = expected_latest_trade_day(now)
    results = [check_freshness(k, expected) for k in keys]
    all_ok = all(r.ok for r in results)

    if checkpoint == "t1":
        # 检查点①：T-1 历史缺仅告警（不熔断——T-1 缺不影响当日 T+1 计划的 T 日数据输入）
        if not all_ok:
            _alert(f"【数据检查点①T-1】部分数据集陈旧/缺失："
                   f"{[r.message for r in results if not r.ok]}，请排查历史采集", "WARN")
        return {"ok": all_ok, "melted": False,
                "details": [r.message for r in results]}

    # checkpoint == "t2"：查 T，FAIL 触发重采窗口
    if all_ok:
        return {"ok": True, "melted": False, "details": [r.message for r in results]}

    # 重采循环：每轮重采失败 key，重检，直到 PASS 或超 deadline
    while _now() < f"{deadline_hour:02d}:00":
        for r in results:
            if r.ok:
                continue
            logger.info("重采 %s（期望 %s，当前最新 %s）", r.key, r.expected_date, r.latest_date)
            ok, msg = _resync_key(r.key)
            if not ok:
                _alert(f"【数据重采】{r.key} 重采失败：{msg}", "WARN")
        # 重检
        results = [check_freshness(k, expected) for k in keys]
        if all(r.ok for r in results):
            return {"ok": True, "melted": False, "details": [r.message for r in results]}
        # 未全 PASS：sleep 15min 再下一轮。
        # why：Tushare 盘后数据落盘有延迟，密集轮询既无效又会撞限频/积分扣减；
        #      brief「每15min重采」即此物理语义（进程内 sleep，schtasks 只负责 18:30 单次拉起）。
        # sleep 前再判一次 deadline：避免最后一轮 sleep 窗口超过熔断截止时间。
        if _now() < f"{deadline_hour:02d}:00":
            time.sleep(15 * 60)

    # 超 deadline 仍 FAIL → 熔断
    melt_msg = (f"【数据熔断】检查点②超时({deadline_hour}:00)仍缺 T 日数据："
                f"{[r.message for r in results if not r.ok]}，eod_plan 将跳过（不交易不自欺）")
    _alert(melt_msg, "ERROR")
    logger.error(melt_msg)
    return {"ok": False, "melted": True, "details": [r.message for r in results]}


def main() -> None:
    """schtasks 入口：python -m scripts.run_data_check t1|t2。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2 or sys.argv[1] not in ("t1", "t2"):
        print("用法: python -m scripts.run_data_check t1|t2", file=sys.stderr)
        sys.exit(1)
    r = run_check(sys.argv[1])
    # 熔断用退出码 2 区分（eod_plan/schtasks 据此跳过）
    sys.exit(2 if r["melted"] else 0)


if __name__ == "__main__":
    main()
