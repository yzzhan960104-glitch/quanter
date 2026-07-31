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
from trading.calendar import expected_latest_trade_day, is_trading_day
from trading.state_store import upsert_data_ready

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


async def pipeline_then_eod(engine) -> None:
    """C-2 事件链：采集 → 等完成 → 按策略声明校验数据 → eod → brief。

    Args:
        engine: 持有 ``async _eod()`` 的交易引擎（编排层只调这一个方法，
            不读引擎内部状态——低耦合）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if not is_trading_day(today):
        logger.info("pipeline_then_eod 跳过：今日非交易日 %s", today)
        return
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
    # 2. 装配本次实验策略 → 收集依赖 key 并集（D3）
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
    expected = expected_latest_trade_day(datetime.now())
    results = {k: check_freshness(k, expected) for k in keys}
    all_ok = all(r.ok for r in results.values())
    # 4. 落就绪事件（供 pre_open 防御性双检）
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
        try:
            from infra.notifier import (build_default_manager, fire_and_forget,
                                        NotificationManager)
            build_default_manager()
            fire_and_forget(
                NotificationManager.get_default().notify_risk_event(msg, "CRITICAL")
            )
        except Exception:
            logger.exception("CRITICAL 告警发送失败")
        return  # 不跑 eod，不产废信号
    # 5. 全绿 → 跑 eod
    await engine._eod()
    # 6. 事件链尾 → Brief 播报（D7）。失败不阻断已完成的 eod plan。
    try:
        from ops.brief_all import run_brief_all
        await run_brief_all()
    except Exception:
        logger.exception("brief 播报失败（不阻断 eod 已完成的 plan）")
