# -*- coding: utf-8 -*-
"""C-8 启动补跑编排：lifespan startup 后台任务，补到「当前可用的一致态」。

物理意图（spec §3.3）：生产机不 7x24，offline 跨任一触发点 → 启动时补跑：
    ① pipeline(D) 未 done 且其 18:00 已过 → 补 采集→校验→data_ready→eod→brief
       （D = expected_latest_trade_day(now) = 最近已收盘交易日）；
    ② plan 日期已过 pre_open 窗口 → run_eod=False 只补数据+brief（政策 A，不产废计划）；
    ③ brief 独立兜底：pipeline done 但 .last_<bot>_brief < D → 补播一次；
    ④ pre_open 窗口 [09:22, ENGINE_PRE_OPEN_CATCHUP_UNTIL) 内且未 done → 补挂单；
      窗口已过且未 done → CRITICAL 知会（政策 A 显式不静默）。

失败语义（spec §3.3 / review 点 1）：补跑任何异常 → 台账 failed + CRITICAL，
不停调度、不阻断 uvicorn——留今晚 18:00 cron 自然收敛（停调度会连收敛机会一起杀掉）。

线程模型：本模块只在 lifespan startup 被 asyncio.create_task 调一次；内部全 async
（pipeline_then_eod 的采集子进程 await proc.wait() 不阻塞事件循环）。
"""
from __future__ import annotations

import logging
import os
from datetime import time
from typing import Optional

from trading import calendar, clock, job_ledger

logger = logging.getLogger(__name__)

# pre_open 补跑窗口起点 = cron 09:22（起点前由 cron 处理，错过 cron 才补）。
WINDOW_START = time(9, 22)


def _catchup_until() -> time:
    """pre_open 补跑窗口截止（HH:MM，env ENGINE_PRE_OPEN_CATCHUP_UNTIL，缺省 10:00）。"""
    raw = os.getenv("ENGINE_PRE_OPEN_CATCHUP_UNTIL", "10:00")
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except Exception:
        logger.warning("ENGINE_PRE_OPEN_CATCHUP_UNTIL=%r 解析失败，回退 10:00", raw)
        return time(10, 0)


def _alert_critical(msg: str) -> None:
    """CRITICAL 钉钉（与 engine._alert_critical 同通道；失败软降级）。"""
    try:
        from infra.notifier import NotificationManager, fire_and_forget
        fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "CRITICAL"))
    except Exception:
        logger.exception("C-8 CRITICAL 告警发送失败（不阻断补跑）：%s", msg)


def _brief_missed(latest_day: str) -> bool:
    """任一 .last_<bot>_brief 文件缺失或内容 < latest_day → 需补播（幂等文件去重）。"""
    from broadcast.__main__ import _read_last, last_brief_file
    for bot in ("trading", "strategy", "data"):
        try:
            last = _read_last(last_brief_file(bot))
        except Exception:
            last = ""
        if not last or last < latest_day:
            return True
    return False


async def _catchup_pipeline(engine) -> bool:
    """补跑 pipeline 链（D=最近已收盘交易日）；返回是否执行了补跑。

    判定（spec §3.3）：D 未 running/done 且（D < today 或 now >= 18:00）→ 补跑；
    D == today 且 now < 18:00 → 不补（今晚 cron 正常处理，避免提前拉未清算数据）。
    run_eod 裁剪：plan_date=next_trading_day(D) ≤ today 且 now > 窗口截止 → False
    （不为已过期交易日产废计划，政策 A）。
    """
    from trading.orchestrate.pipeline import pipeline_then_eod
    now = clock.now()
    today = clock.today()
    d = calendar.expected_latest_trade_day(now)
    status = job_ledger.latest_status("pipeline", d)
    if status in ("running", "done"):
        logger.info("C-8 pipeline 补跑跳过：%s 已 %s", d, status)
        return False
    if d == today and now.time() < time(18, 0):
        logger.info("C-8 pipeline 补跑跳过：%s 今晚 18:00 cron 将处理", d)
        return False
    plan_date = calendar.next_trading_day(d)
    run_eod = not (plan_date <= today and now.time() > _catchup_until())
    logger.warning("C-8 启动补跑 pipeline：D=%s plan_date=%s run_eod=%s",
                   d, plan_date, run_eod)
    await pipeline_then_eod(engine, for_date=d, run_eod=run_eod)
    return True


async def _catchup_brief(latest_day: str) -> bool:
    """brief 独立兜底：pipeline done 但幂等文件 < D → 补播一次。"""
    if job_ledger.latest_status("pipeline", latest_day) != "done":
        return False
    if not _brief_missed(latest_day):
        return False
    from ops.brief_all import run_brief_all
    logger.warning("C-8 启动补跑 brief：D=%s（.last_*_brief 缺失/陈旧）", latest_day)
    await run_brief_all()
    return True


async def _catchup_pre_open() -> tuple[bool, str]:
    """补跑 pre_open（今日窗口内且未 done）；窗口已过且未 done → CRITICAL 知会。"""
    from trading.engine import pre_open
    now = clock.now()
    today = clock.today()
    if not calendar.is_trading_day(today):
        return False, "非交易日"
    status = job_ledger.latest_status("pre_open", today)
    if status in ("running", "done"):
        return False, f"已 {status}"
    until = _catchup_until()
    t = now.time()
    if t < WINDOW_START:
        return False, "窗口未开始（09:22 cron 将处理）"
    if t >= until:
        msg = f"C-8 pre_open 窗口已过（now={t:%H:%M} >= {until:%H:%M}），今日不补挂单"
        logger.warning(msg)
        _alert_critical(msg)
        return False, msg
    logger.warning("C-8 启动补跑 pre_open：today=%s", today)
    await pre_open(today)
    return True, "已补跑"


async def run_startup_catchup(engine) -> dict:
    """lifespan startup 后台补跑编排（幂等，只补最近一致态）。

    Returns:
        {"pipeline": bool, "brief": bool, "pre_open": bool,
         "pre_open_note": str, "error": str | None}
    """
    result = {"pipeline": False, "brief": False, "pre_open": False,
              "pre_open_note": "", "error": None}
    try:
        job_ledger.init_db()
        job_ledger.reset_stale_running()
    except Exception:
        logger.exception("C-8 台账初始化失败（跳过本次补跑）")
        result["error"] = "台账初始化失败"
        return result
    try:
        latest_day = calendar.expected_latest_trade_day(clock.now())
        result["pipeline"] = await _catchup_pipeline(engine)
        result["brief"] = await _catchup_brief(latest_day)
        result["pre_open"], result["pre_open_note"] = await _catchup_pre_open()
    except Exception as e:
        logger.exception("C-8 启动补跑失败")
        _alert_critical(f"C-8 启动补跑失败：{e}")
        result["error"] = str(e)
    return result