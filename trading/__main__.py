# -*- coding: utf-8 -*-
"""二期自动交易引擎独立常驻进程入口：``python -m trading``。

============================================================================
Why 独立进程（Task5/9 风险官硬约束 · 绝对红线）
============================================================================
本入口起一个独立常驻 Python 进程，**不寄生 server uvicorn**：

- ``trading.dynamic_whitelist._DYNAMIC`` 是模块级全局（当日计划标的临时注入），
  只在 engine 进程内有效（设计预期，见 ``dynamic_whitelist.py`` 模块 docstring）。
- 若 engine 与 server 同进程：engine 在 pre_open 注入的 _DYNAMIC 会污染 server 的
  手动下单路径（Cockpit/前端），导致 server 手动下单越过静态 env 白名单（前视污染），
  破坏「server 行为与改造前完全一致」的向后兼容红线。
- 因此 server 的 lifespan **不** import 本模块、不构造 TradingEngine；入口唯一在此。

职责切分（薄入口原则 · Karpathy 极简）：
- 本入口只做三件事：① 加载 .env ② 起 event loop 守护 AsyncIOScheduler
  ③ LIVE 模式启动期 ≥5 天影子期硬闸（Plan 4 T6，``check_shadow_gate`` fail-closed
  真·闸，取代历史 WARNING 提醒——spec §5.3 误称"硬闸"实为提醒）。
- 全部业务逻辑（四触发点、APScheduler cron 装配、交易日判定、影子分流）都在
  ``trading/engine.py::TradingEngine``（Task9），本入口不重复实现任何业务逻辑。

============================================================================
⚠️ Scope 边界：本入口【不】做策略层数据源注入
============================================================================
本入口只起 APScheduler 常驻进程；四触发点的真实数据源属「二期引擎上线集成」
阶段的工作（SOP/follow-up），不在 Task 10/11 代码 scope：

- ``NecklineMethodStrategy.scan_at`` 扫颈线法信号（eod_plan 消费）
- 持仓状态机 ``stop_prices`` map（stop_loss_monitor 消费）
- ``active.json`` 真实 local_positions（post_close 对账消费）

Task 9 的四个内部触发方法（``_eod/_pre_open/_stoploss/_post_close``）已是
**安全 no-op**：先过 ``calendar.is_trading_day`` 判交易日，再 logger.info 触发
记录，数据源为 None/空时优雅降级不崩。故 __main__ 起进程后 APScheduler 即便
触发这四个 job 也不会崩。

详见 ``docs/superpowers/plans/2026-07-21-auto-trading-engine.md`` Task 11 SOP
+ ledger 必修清单（策略层→引擎层信号源集成 = 二期引擎上线集成阶段）。

============================================================================
Windows 进程托管
============================================================================
本入口是前台进程（stdout 日志），设计成可被 schtasks / PM2 / terminal tab 托管：
- Task 11 的 ``run_trading_engine.bat`` 会调它（schtasks 注册开机自启）。
- Ctrl-C（KeyboardInterrupt）→ 优雅 ``eng.shutdown()``（APScheduler ``wait=False``
  不等 pending job，进程退出场景）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

# 加载 .env（Task4 已装 python-dotenv；环境无 dotenv 时 fallback 跳过，env 由
# 外层 schtasks/PM2 注入亦可——本行只是开发便利，非业务依赖）。
try:
    from dotenv import load_dotenv

    # override=True：.env 是单一真相源，强制覆盖系统/session env（修：历史 AUTO_TRADE_MODE
    # 被 Windows 系统 env 压制，致 .env 切 live 不生效——engine 仍读继承的 dry_run）。
    load_dotenv(override=True)
except ImportError:
    pass

# 中文友好日志格式：asctime + levelname + logger name + message。
# level=INFO：启动/触发记录可见；DEBUG 太吵（APScheduler 内部日志量大）。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 顶层绑定 resolve_active 到本模块属性（供 check_shadow_gate 调用 + 测试 monkeypatch 覆盖）。
# Why 不在 check_shadow_gate 内用 ``from experiment.resolver import resolve_active``：
# 那会把符号绑到 experiment.resolver 模块，测试 ``monkeypatch.setattr(trading.__main__,
# "resolve_active", fake)`` 无法覆盖（命名空间分叉）→ 测试无法 mock DB。
# experiment.resolver 依赖链纯标准库（sqlite3/dataclass），不拉 trading.engine 的
# apscheduler 重链，顶层 import 安全（不破坏 __main__ 模块加载性能 / 测试隔离）。
from experiment.resolver import resolve_active


def _days_since_activation(activated_at):
    """ISO activated_at 距今自然日数（影子期校验）。

    None/解析失败 → 返 None（调用方保守视为"影子期不足"拒绝，D9 宁可误杀）。
    experiment activated_at 由 cli._now()=datetime.now().isoformat 落（本地 naive），
    datetime.now() 同口径，差值正确（无时区/UTC 偏移分叉）。
    """
    from datetime import datetime
    if not activated_at:
        return None
    try:
        then = datetime.fromisoformat(activated_at).date()
    except (ValueError, TypeError):
        # 解析失败保守拒绝：宁可误杀也不放行未校验过的实验进 LIVE。
        return None
    return (datetime.now().date() - then).days


def check_shadow_gate() -> bool:
    """≥5 天影子期硬闸（spec §5.3/§12#14，Plan 4 L5，fail-closed 真·闸）。

    物理意图：spec §5.3 把 ``__main__`` 旧 WARNING 误称"≥5天硬闸"——实为提醒，切
    LIVE 只需改 env。本函数升级为真闸：mode=live 时查所有 ACTIVE 实验的
    activated_at，任一影子期 < TRADE_SHADOW_MIN_DAYS → 返 False + 钉钉
    CRITICAL（绝不裸跑真单）。

    W2 改造（C-2 scheduling-orchestration Task 5）：原 ``_shadow_gate`` 内联
    ``sys.exit(2)`` 在独立进程模式可接受，但 engine 合并进 uvicorn server 进程后，
    ``sys.exit`` 会杀掉整个 API server。故本函数从「进程级 sys.exit」收敛为「函数级
    布尔返回」：拒切 LIVE 时 ``return False`` 而非 sys.exit。独立 ``__main__`` 模式
    仍可在调用点 ``if not check_shadow_gate(): sys.exit(2)``（进程级决策由调用方决定），
    uvicorn lifespan 则据返回值决定是否起 engine。CRITICAL 钉钉告警（build_default_manager
    + fire_and_forget）行为不变。

    风控红线（D3/D8/D9）：
      - mode=dry_run → 直接放行返 True（硬闸仅 LIVE 触发，影子模式不拦）。
      - resolve_active 抛异常 → fail-closed 返 False（绝不因查不到而放行 LIVE；
        区别于返空列表的合法清场——异常意味着状态未知，按未知拒绝）。
      - 返空列表 → 放行（D8 合法清场：实验被 archive 后无在线版本，是研究员显式
        下线操作的结果，非查询失败，不应阻塞 LIVE）。
      - activated_at 缺失(None)或解析失败 → 保守归入 fresh 拒绝（D9 宁可误杀：
        历史脏数据 activated_at 缺失比误放行一个未观测满期的新实验代价小得多）。

    通道装配（关键）：trading/__main__ 是独立常驻进程（不寄生 server lifespan），
    grep 确认 presentation/server/main.py、discovery/cli.py 均在启动期显式 build_default_manager()
    装钉钉通道，但本入口历史从未装过。本函数在调 notify_risk_event 前补装——否则
    拒切 LIVE 的 CRITICAL 告警会因无通道走软降级静默丢失，返 False 仍生效但研究
    员不知为何被拒。build_default_manager 幂等，重复调用安全。
    """
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    if mode == "dry_run":
        return True   # 影子模式不拦（硬闸仅 LIVE 触发）
    min_days = int(os.getenv("TRADE_SHADOW_MIN_DAYS", "5"))
    try:
        experiments = resolve_active()
    except Exception as e:
        # 查询失败 fail-closed（绝不因查不到而放行 LIVE）。
        # 通道装配放最前：告警前必须确保钉钉通道已就绪，否则走软降级静默丢失。
        try:
            from infra.notifier import build_default_manager, NotificationManager, fire_and_forget
            build_default_manager()   # 幂等；trading/__main__ 历史未装，此处补装
            fire_and_forget(NotificationManager.get_default().notify_risk_event(
                f"拒切 LIVE：experiment 状态查询失败（{e}），回退 dry_run", "CRITICAL"))
        except Exception:
            pass
        return False
    # 空列表放行（D8）；任一影子期不足/缺失 → 拒绝（D9）。
    # 两次 _days_since_activation 调用合并到 fresh 推导式：None 视为 fresh（保守拒绝）。
    fresh = [e for e in experiments
             if _days_since_activation(getattr(e, "activated_at", None)) is None
             or _days_since_activation(e.activated_at) < min_days]
    if fresh:
        try:
            from infra.notifier import build_default_manager, NotificationManager, fire_and_forget
            build_default_manager()
            fire_and_forget(NotificationManager.get_default().notify_risk_event(
                f"拒切 LIVE：{len(fresh)} 实验影子期不足 {min_days} 天，回退 dry_run", "CRITICAL"))
        except Exception:
            pass
        return False
    # LIVE 放行前的最后提醒（保留原 WARNING 语义，避免运维因闸门通过而放松对账/网关/
    # 止损行情源就绪的人工核验——闸只校验影子期，不校验对账连续无 drift 等其他红线）。
    logger.warning("⚠️ LIVE 模式：所有 ACTIVE 实验影子期 ≥ %s 天，放行（确保对账/网关/止损已就绪）", min_days)
    return True


def log_startup_banner():
    """M3：启动 banner 打印进程内关键配置 + 口径版本（配置漂移一眼可见）。

    Why：[[qmt-connect-1-rootcause]] 故障中 engine 进程内 session=123456 而 .env=123458，
    无 banner 无人发现。本函数把进程启动时读到的 env 固化进日志，对比 .env 即知漂移。

    物理意图（纯函数 + caplog 单测友好）：
      - 仅读 os.environ + logger.info，无 gateway/scheduler 依赖，便于单测断言；
      - 漂移四要素：session_id / account_id / userdata_path / mode + confirm，
        覆盖 connect/login 易漂移的全部 QMT/模式 env；
      - 口径版本：eod=next_trading_day, pre_open=today（标的 T+1 对齐，T+0 则漏挂/重挂）。
    """
    logger.info(
        "=== 启动 banner === session=%s account=%s userdata=%s mode=%s confirm=%s | "
        "口径: eod=next_trading_day, pre_open=today（标的 T+1 对齐）",
        os.environ.get("QMT_SESSION_ID", "?"),
        os.environ.get("QMT_ACCOUNT_ID", "?"),
        os.environ.get("QMT_USERDATA_PATH", "?"),
        os.environ.get("AUTO_TRADE_MODE", "?"),
        os.environ.get("AUTO_CONFIRM_PLAN", "?"),
    )


async def _run_forever() -> None:
    """起 TradingEngine + 守护 event loop（APScheduler 后台跑四 cron）。

    Why ``while True: await asyncio.sleep(3600)`` 而非 ``await eng.sched.running``
    这种事件等待：APScheduler 的 AsyncIOScheduler 在后台协程内跑 job 调度，主协程
    只需「挂起不退出」即可；每小时醒一次无业务意义（仅保活心跳，避免某些事件循环
    实现对纯阻塞 sleep 的超时打断异常）。 KeyboardInterrupt/CancelledError 由外层
    ``asyncio.run`` 冒泡到 ``__main__`` 守卫统一处置。

    W3（C-2 scheduling-orchestration Task 5）：原 7 步 I/O 初始化（connect + 注册回调
    + position_book.init_db + state_store.init_store + _migrate_env_to_account）已收口到
    ``TradingEngine.bootstrap()``，本入口仅做「构造 → bootstrap → start → 守护」四段，
    让独立进程与 uvicorn server lifespan 复用同一段初始化（不改启动语义：connect-then-start
    顺序保持不变，只是搬进 bootstrap）。
    """
    # 惰性 import：避免模块顶层 import 触发 trading 包重链（test 导入本模块时不
    # 应连带拉起 engine 依赖链；engine.py 顶层 import apscheduler 等）。
    from trading.engine import TradingEngine

    # M3 启动 banner：在 bootstrap（含网关 connect）前打印进程内 session/account/mode/
    # 口径版本（banner 先于 connect 输出，便于排查 .env 漂移）。
    log_startup_banner()

    eng = TradingEngine()

    # W3：7 步 I/O 初始化收口进 bootstrap（connect + 回调注册 + position_book/state_store
    # 建表迁移）。必须先于 eng.start()——APScheduler 一旦 start，触发点可能读写 DB/回调链路。
    await eng.bootstrap()

    eng.start()  # 注册四 cron job + 启动 AsyncIOScheduler（不阻塞）
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl-C / 外部 cancel：优雅 shutdown APScheduler（wait=False 不等 pending job）。
        eng.shutdown()


if __name__ == "__main__":
    # 启动期模式读取（默认 dry_run · 影子红线）。
    # 缺省 dry_run：未显式 AUTO_TRADE_MODE=live 一律按影子处理，宁可漏挂单也
    # 不在未观测足够天数时盲发真单。
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    logger.info("=== 自动交易引擎启动（AUTO_TRADE_MODE=%s）===", mode)

    # Plan 4 T6：≥5 天影子期硬闸（fail-closed 真·闸）。
    # 取代原启动期 WARNING 段（spec §5.3 误称"硬闸"实为提醒——切 LIVE 只需改 env，
    # 无任何拦单）。check_shadow_gate 内部 dry_run 直接放行；live 时查所有 ACTIVE 实验
    # activated_at，任一影子期 < TRADE_SHADOW_MIN_DAYS → 返 False + 钉钉 CRITICAL。
    # 异常 fail-closed / 空列表放行 / activated_at 缺失保守拒绝（D3/D8/D9）。
    # W2（C-2 Task 5）：check_shadow_gate 返 bool 而非 sys.exit——独立进程模式下
    # 拒切 LIVE 仍 sys.exit(2)（进程级决策）；engine 合并进 uvicorn 后由 server
    # lifespan 据 bool 决定是否起 engine，不再 sys.exit 杀掉整个 API server。
    if not check_shadow_gate():
        logger.error("影子期不足，拒绝启动 engine（独立进程模式退出）")
        sys.exit(2)   # 独立进程模式仍可 exit；uvicorn 模式由 lifespan 决定

    try:
        asyncio.run(_run_forever())
    except KeyboardInterrupt:
        # Ctrl-C 在 asyncio.run 外层再次被捕（双保险）。
        logger.info("收到 Ctrl-C，进程退出。")
        sys.exit(0)
