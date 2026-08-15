# -*- coding: utf-8 -*-
"""C-2 事件链编排：采集 → 等完成 → 按策略声明校验数据 → eod → brief。

物理定位（Layer2 编排层）：只连线不判定。采集/freshness/eod/brief 各自归位，
本函数把它们按事件顺序串起来，取代"19:00 时钟赌博"——用确定性的 ``await proc.wait()``
取代靠时差猜测采集是否完成的脆弱时序。

事件链（每步等完才下一步）：
    ① ``asyncio.create_subprocess_exec(ops/data_pipeline.py)`` → ``await proc.wait()``
    ② 装配本次在线实验策略 → 收集 ``required_data_keys`` 并集（D3）
    ③ 按声明的 key 逐个 ``check_freshness``（纯函数，不读旧 parquet mtime）
    ④ 落 ``data_ready`` 就绪事件（供 pre_open 防御性双检）
    ⑤ 全绿 → ``engine._eod()``；否则 CRITICAL 告警 + 跳过 eod（不产废信号）
    ⑥ 事件链尾 → brief 播报（失败不阻断已完成的 eod plan）

C-8 补跑参数化（spec §3.2）：
    for_date = 事件链数据日（补跑传最近已收盘交易日 T）；run_eod = 是否产计划
    （窗口已过传 False 只补数据+brief，政策 A 不产过期计划）。默认 None/True 与 C-2 行为等价。
台账（spec §3.4）：pipeline 状态 running→done/failed 由本函数统一落，
    cron 与启动补跑共用（先查后写防双跑）。

依赖单向（低耦合硬约束）：只 import ``data.freshness``（纯函数）+ 标准库 +
``engine._eod`` + ``state_store`` + ``calendar`` + ``experiment.resolver`` /
``strategies.registry``，**绝不反向 import server/broadcast**（广播收口在
brief 步骤通过 ``ops.brief_all`` 间接调用，编排层本身不感知广播实现）。

模块级 import 的可测性考量：``check_freshness`` / ``resolve_active`` /
``build_strategy`` 故意提到模块顶部——测试用 ``patch("trading.orchestrate.pipeline
.<name>")`` 替换时，patch 路径必须落在被测模块的属性命名空间上，函数内 import
会让 patch 目标落到原始模块从而失效。``asyncio`` 同理（测试 patch
``...pipeline.asyncio.create_subprocess_exec``）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from data.freshness import check_freshness
from experiment.resolver import resolve_active
from strategies.registry import build_strategy
from trading.calendar import expected_latest_trade_day, is_trading_day, next_trading_day
from trading import clock, job_ledger
from trading.state_store import upsert_data_ready

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def _ledger_finish(today: str, status: str, message: str = "") -> None:
    """pipeline 台账落终态（失败软降级，绝不阻断事件链主流程）。"""
    try:
        job_ledger.finish_run("pipeline", today, status, message)
    except Exception:
        logger.exception("job_ledger finish_run 失败（不阻断主流程）")


def _notify_risk(msg: str, level: str, *, soft: bool = False) -> None:
    """风险事件播报单源（评审 2026-08-15 去重：数据未就绪 / regime 停手两处共用）。

    soft=True 走 debug 软降级（「停手」这类主行为已生效，播报失败仅留痕——
    与 CRITICAL 级「告警本身是主行为」的 exception 语义区分）。
    """
    try:
        from infra.notifier import (build_default_manager, fire_and_forget,
                                    NotificationManager)
        build_default_manager()
        fire_and_forget(
            NotificationManager.get_default().notify_risk_event(msg, level))
    except Exception:
        if soft:
            logger.debug("播报软降级", exc_info=True)
        else:
            logger.exception("告警发送失败")


async def pipeline_then_eod(engine, *, for_date: str | None = None,
                            run_eod: bool = True) -> None:
    """C-2 事件链：采集 → 等完成 → 按策略声明校验数据 → eod → brief。

    Args:
        engine: 持有 ``async _eod()`` 的交易引擎（编排层只调这一个方法，
            不读引擎内部状态——低耦合）。
        for_date: 补跑用——事件链数据日（YYYY-MM-DD，缺省=clock.today()）。
            C-8 spec §3.2：T+1 早上补跑 T 日链时，data_ready 必须落 T、
            eod 必须产 next_trading_day(T) 计划，否则日期错位（C-6 同源风险）。
        run_eod: 补跑窗口已过时传 False——只补 采集→校验→data_ready→brief，
            不为已过期交易日产废计划（政策 A，spec §2）。
    """
    today = for_date or clock.today()
    if not is_trading_day(today):
        logger.info("pipeline_then_eod 跳过：非交易日 %s", today)
        return
    _st = job_ledger.latest_status("pipeline", today)
    if _st in ("running", "done"):
        logger.info("pipeline_then_eod 跳过：%s 已 %s（台账守卫，cron/补跑不双跑）",
                    today, _st)
        return
    try:
        job_ledger.begin_run("pipeline", today, clock.now().isoformat())
    except Exception:
        logger.exception("job_ledger begin_run 失败（不阻断主流程）")
    try:
        # 1. 采集子进程（原 ops/data_pipeline.py，T1→采→T2）
        log_path = ROOT / "logs" / "data_pipeline.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # M2：本编排现跑在长生命周期 uvicorn 进程内（不再是短命 schtasks 进程），裸
        # ``open(...,"ab")`` 作为 subprocess stdout 会让文件句柄泄漏累积（每天 +1）。显式
        # 捕获到局部变量，await proc.wait() 后 close()——确保句柄确定性地归还 OS。
        log_fh = open(log_path, "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(ROOT / "ops" / "data_pipeline.py"), cwd=str(ROOT),
                stdout=log_fh,
                stderr=asyncio.subprocess.STDOUT,
            )
            rc = await proc.wait()
        finally:
            log_fh.close()
        # C-4 U3c：采集子进程失败（rc!=0）= T 日增量未落湖 → 用 T-1 数据算 T+1 计划 = 时序 bug
        # （[[eod-date-offbyone-fix]] 同源风险）。升 L1：raise _CriticalHalt → engine _halt 停调度，
        # 绝不用陈旧数据产废信号（spec §3 pipeline 采集失败=L1）。
        # C-8 V2：抛前落台账 failed（补跑路径由 catchup 捕获转 failed+CRITICAL，不停调度）。
        # 函数内 import 规避循环依赖（engine.py 顶层 import 本编排层时会反向引用）。
        if rc != 0:
            _ledger_finish(today, "failed", f"采集子进程失败 rc={rc}")
            # W1-B（Task 10）：_CriticalHalt 直 import 物理真身 trading.critical（engine
            # re-export 垫层已删）。保函数内 lazy 规避循环依赖（本编排层被 engine 引用）。
            from trading.critical import _CriticalHalt
            raise _CriticalHalt(f"采集子进程失败 rc={rc}（T 日增量未落湖，拒产 T+1 计划）")
        # 2. 装配本次在线实验策略 → 收集依赖 key 并集（D3）
        keys: set[str] = set()
        try:
            for exp in resolve_active():
                strat = build_strategy(exp.strategy_name, exp.params)
                keys |= set(strat.required_data_keys)
        except Exception:
            logger.exception("策略依赖解析失败，回退默认 {daily}")
            keys = {"daily"}
        keys = keys or {"daily"}
        # 3. 按声明的 key 逐个校验（复用 check_freshness 纯函数，不读旧 parquet mtime）
        # C-6 V3：单一时间源（时点传 expected_latest_trade_day，clock.now() 返 datetime 等价）。
        expected = expected_latest_trade_day(clock.now())
        results = {k: check_freshness(k, expected) for k in keys}
        all_ok = all(r.ok for r in results.values())
        # 4. 落就绪事件（供 pre_open 防御性双检）——C-8 V2：日期用 today（for_date），
        #    补跑时落 T 而非今天，否则 pre_open gate 查 expected_latest_trade_day=T 永远 None。
        for k, r in results.items():
            try:
                upsert_data_ready(today, k, ok=r.ok,
                                  melted=(not all_ok and rc != 0),
                                  latest_date=r.latest_date, expected_date=expected,
                                  message=r.message)
            except Exception:
                logger.exception("data_ready 落库失败（不阻断）")
        if not all_ok:
            msg = f"数据未就绪：{[r.message for r in results.values() if not r.ok]}，eod 跳过"
            logger.warning(msg)
            _ledger_finish(today, "failed", msg)
            _notify_risk(msg, "CRITICAL")
            return  # 不跑 eod，不产废信号
        # 3.5 T13-B #1：连续性 scan（freshness 全绿后，eod 前）—— 历史缺口检测（与 freshness
        # 实时性互补）。scan FAIL **不阻断 eod**（历史缺口与当日交易无关，blueprint §2.3），
        # 仅触发异步 repair 子进程（fire-and-forget；配额/熔断在 repair_gaps --auto 内）。
        # N1 语义对齐（2026-08-16）：unjustified_gaps 已升级为「日级判定 + 长洞市场共识
        # + unfillable sidecar 排除后真需补的子段计数」——旧段级 all() 误报（16,371 段）
        # 收敛到真缺子段，本闸触发频率随之对齐真值（消费 key 不变）。
        try:
            from data.tools.scan_integrity import scan as _scan_integrity
            _scan_report = _scan_integrity(lake_dir=str(ROOT / "data_lake"))
            _n_gaps = _scan_report.get("unjustified_gaps", 0)
            if _n_gaps > 0:
                logger.warning("T13-B scan 发现 %d 个待补子段（%d 标的，日级判定后），触发异步补采",
                               _n_gaps, len(_scan_report.get("unjustified_symbols", [])))
                import subprocess as _sp
                _log_dir = ROOT / "logs"
                _log_dir.mkdir(parents=True, exist_ok=True)
                _log_fh = (_log_dir / "repair_auto.log").open("ab")
                _sp.Popen([sys.executable, "-m", "data.tools.repair_gaps", "--auto",
                           "--lake-dir", str(ROOT / "data_lake")],
                          cwd=str(ROOT), stdout=_log_fh, stderr=_sp.STDOUT,
                          stdin=_sp.DEVNULL, close_fds=True)
                # _log_fh 由子进程继承，退出后 OS 回收（与 _run_discovery_subprocess 同模式）
        except Exception:
            logger.exception("T13-B scan/repair 触发异常（不阻断 eod）")
        # 5. 全绿 → 跑 eod（C-8 V2：补跑传显式 data_day/plan_date；默认路径零变化）
        if run_eod:
            # 4.5 A1 前置（DG-G4 · 2026-08-14）：空头/未知环境不产新计划（停手≠清仓，
            # 存量持仓的退出链路不受影响）。与 _pre_open_gate ④ 段同一 _regime_gate
            # 单源（当日缓存，不重复读湖）；BEAR/UNKNOWN 均 fail-closed 停产。
            rg_ok, rg_reason = await engine._regime_gate()
            if not rg_ok:
                msg = f"eod 跳过：{rg_reason}"
                logger.warning(msg)
                _ledger_finish(today, "skipped", rg_reason)
                # soft=True：播报软降级（通知通道故障不能阻断「停手」这个主行为）
                _notify_risk(msg, "WARN", soft=True)
                return  # 停产 T+1 计划；brief 播报跳过（eod 未跑，无盘后面板）
            if for_date is not None:
                await engine._eod(data_day=today, plan_date=next_trading_day(today))
            else:
                await engine._eod()
        # 6. 事件链尾 → Brief 播报（D7）。失败不阻断已完成的 eod plan。
        try:
            from ops.brief_all import run_brief_all
            await run_brief_all()
        except Exception:
            logger.exception("brief 播报失败（不阻断 eod 已完成的 plan）")
        _ledger_finish(today, "done")
    except Exception:
        _ledger_finish(today, "failed", "未预期异常")
        raise