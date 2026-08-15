# -*- coding: utf-8 -*-
"""C-8 启动补跑编排：lifespan startup 后台任务，补到「当前可用的一致态」。

物理意图（spec §3.3）：生产机不 7x24，offline 跨任一触发点 → 启动时补跑：
    ① pipeline(D) 未 done 且其 18:00 已过 → 补 采集→校验→data_ready→eod→brief
       （D = expected_latest_trade_day(now) = 最近已收盘交易日）；
    ② plan 日期已过 pre_open 窗口 → run_eod=False 只补数据+brief（政策 A，不产废计划）；
    ③ brief 独立兜底：pipeline done 但 brief_<bot> 台账非 done → 补播一次；
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
    """任一 brief_<bot> 台账非 done → 需补播（B4 幂等查 job_ledger）。

    取代旧 .last_<bot>_brief 文件口径（B4 SSoT 收口）：
      - 旧：文件缺失或内容 < latest_day → 补播
      - 新：job_ledger.latest_status(`brief_<bot>`, latest_day) != "done" 任一为真 → 补播
    跨进程一致：sqlite 共享真相源取代文件锁；job_name 与 broadcast._main_push 同口径。
    """
    for bot in ("trading", "strategy", "data"):
        try:
            if job_ledger.latest_status(f"brief_{bot}", latest_day) != "done":
                return True
        except Exception:
            # 台账读异常（DB 损坏）→ 视为需补播（保守不漏播；最坏重复推一次）
            logger.warning("查 brief_%s 台账失败 date=%s（视为需补播）",
                           bot, latest_day, exc_info=True)
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
    """brief 独立兜底：pipeline done 但 brief 台账非 done → 补播一次。

    W5 对账（spec #13）：pipeline done 后补一道 get_ready 校验——若台账 done 但
    data_ready 内容未绿（采集完成了但内容校验失败/未落盘），warning 显式暴露漂移。
    不阻断补播（brief 本身是观测通道，把漂移信号推给研究员比静默更有价值）。
    """
    if job_ledger.latest_status("pipeline", latest_day) != "done":
        return False
    # W5：台账 done 后对账 data_ready + job_ledger 单口判定，暴露「台账 done、内容缺」漂移。
    # 物理意图：spec #13 三张嘴之一就是「台账 done、内容缺」——catchup 在 pipeline done
    # 后补播前是发现此漂移的最佳窗口（再晚就要等 pre_open gate 拒单才发现）。
    from trading.state_store import get_ready
    try:
        if not get_ready(latest_day):
            logger.warning(
                "C-8 brief 补跑前对账漂移：pipeline done 但 get_ready=False date=%s"
                "（data_ready 内容未绿/未落盘，详见 get_ready warning）", latest_day)
    except Exception:
        # get_ready 异常不阻断补播（守 C-4 软降级；对账是附加防护非主流程）
        logger.exception("C-8 brief 补跑前 get_ready 对账异常 date=%s（不阻断补播）",
                         latest_day)
    if not _brief_missed(latest_day):
        return False
    from ops.brief_all import run_brief_all
    logger.warning("C-8 启动补跑 brief：D=%s（brief_*_brief 台账非 done）", latest_day)
    await run_brief_all()
    return True


async def _catchup_pre_open(ports=None) -> tuple[bool, str]:
    """补跑 pre_open（今日窗口内且未 done）；窗口已过且未 done → CRITICAL 知会。

    Args:
        ports: T1 缝合点 #1——engine 实例特有依赖（gate + 动态白名单）。由调用方
            （``run_startup_catchup(engine)`` / ``engine._health_guard``）传 ``engine._ports``，
            让补跑 pre_open 与 cron 路径一样经三段闸（行为等价原 ``_ACTIVE_ENGINE`` 全局置位）。
    """
    # W1-B（Task 10）：直 import 物理真身 phases.pre_open（engine re-export 垫层已删）。
    # 保函数内 lazy：catchup 被引擎补跑/health_guard 高频调，顶层拉 phases 链非必要；
    # 调用时经 phases.pre_open 模块属性解析，patch("trading.phases.pre_open.pre_open") 命中。
    from trading.phases.pre_open import pre_open
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
    await pre_open(today, ports=ports)
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
        result["pre_open"], result["pre_open_note"] = await _catchup_pre_open(
            ports=getattr(engine, "_ports", None))
    except Exception as e:
        logger.exception("C-8 启动补跑失败")
        _alert_critical(f"C-8 启动补跑失败：{e}")
        result["error"] = str(e)
    return result