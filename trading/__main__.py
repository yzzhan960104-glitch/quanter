# -*- coding: utf-8 -*-
"""二期自动交易引擎常驻进程入口：``python -m trading``。

============================================================================
定位（C-5 V1 进程模型统一后）
============================================================================
本入口起 **uvicorn server**（``presentation.server.main:app``），engine 由 server
lifespan 装配——与生产 ``start_all.py`` 链**同一入口**，消除历史双进程抢 session
（C-5 V1，[[qmt-connect-1-rootcause]] 教训）。端口 8000 天然单例（第二实例 bind
失败 exit），无需文件锁。``run_server()`` 是薄包装（live 不 reload，防 reloader
子进程抢 session）；``check_shadow_gate`` 影子期检查收归 lifespan（本块不 sys.exit）。

Why engine 可合并进 lifespan（W1 实例属性隔离取代旧进程隔离硬约束）：
- 历史红线「engine 必须独立进程、绝不嵌入 server」是为了防前视污染（engine 注入的
  动态白名单污染 server 手动下单路径）。W1 已把动态白名单从模块级全局
  ``_DYNAMIC`` 改为 engine **实例属性** ``_dynamic_whitelist``，``_submit`` 经
  ``_ACTIVE_ENGINE`` 单例反查实例、显式透传 ``whitelist`` 给 submit_order；server 路径
  不传 whitelist 即走纯 env 旧路径（见 ``engine.py`` 模块 docstring 不变量块）。
- 因此 engine 与 server 同进程不再前视污染，合并进 uvicorn lifespan 安全。

职责切分（薄入口原则 · Karpathy 极简）：
- 本入口只做两件事：① 加载 .env（``load_dotenv(override=True)``）② 调 ``run_server()``
  起 uvicorn。全部业务逻辑（四触发点 cron、APScheduler 装配、交易日判定、影子分流、
  网关 connect/bootstrap）都在 lifespan + ``trading/engine.py::TradingEngine``，本入口
  不重复实现任何业务逻辑。
- ``check_shadow_gate`` 影子期硬闸由 lifespan（main.py:196 ``if check_shadow_gate():
  eng.start()``）决策；影子期不足时 server 仍起（手动 API 可用），仅 scheduler 不 start。

============================================================================
⚠️ Scope 边界：本入口【不】做策略层数据源注入
============================================================================
本入口只起 uvicorn server（engine 由 lifespan 装）；四触发点的真实数据源属「二期引擎
上线集成」阶段的工作（SOP/follow-up），不在 C-5 scope：

- ``NecklineMethodStrategy.scan_at`` 扫颈线法信号（eod_plan 消费）
- 持仓状态机 ``stop_prices`` map（stop_loss_monitor 消费）
- ``active.json`` 真实 local_positions（post_close 对账消费）

Task 9 的四个内部触发方法（``_eod/_pre_open/_stoploss/_post_close``）已是
**安全 no-op**：先过 ``calendar.is_trading_day`` 判交易日，再 logger.info 触发
记录，数据源为 None/空时优雅降级不崩。故 server 起后 APScheduler 即便
触发这四个 job 也不会崩。

详见 ``docs/superpowers/plans/2026-07-21-auto-trading-engine.md`` Task 11 SOP
+ ledger 必修清单（策略层→引擎层信号源集成 = 二期引擎上线集成阶段）。

============================================================================
Windows 进程托管
============================================================================
``run_server()`` 起 uvicorn（前台进程，stdout 日志），设计成可被 schtasks / PM2 /
「启动」文件夹快捷方式托管：
- ``scripts/run_trading_engine.bat`` 调 ``python -m trading``（schtasks 注册开机自启，
  命令字面不变，内部起 server）。
- Ctrl-C（KeyboardInterrupt）→ uvicorn lifespan shutdown 钩子优雅停 TradingEngine
  scheduler + 断网关（main.py lifespan yield 后的销毁段，``sched.shutdown(wait=False)``
  不等 pending job）。
"""
from __future__ import annotations

import logging
import os

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
# C-6 V3：单一时间源（_days_since_activation 时点走 clock.now，与 engine.py V2 同款）。
# 安全性：clock.py 只依赖 trading.calendar，不反向 import __main__，无循环 import。
from trading import clock


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
    return (clock.now().date() - then).days


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

    通道装配（关键）：trading/__main__ 作为开发/调试常驻入口（生产路径在 uvicorn lifespan
    内起 engine，本入口不寄生 server lifespan），grep 确认 presentation/server/main.py、
    discovery/cli.py 均在启动期显式 build_default_manager() 装钉钉通道，但本入口历史从未装过。
    本函数在调 notify_risk_event 前补装——否则拒切 LIVE 的 CRITICAL 告警会因无通道走软降级
    静默丢失，返 False 仍生效但研究员不知为何被拒。build_default_manager 幂等，重复调用安全。
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


def run_server() -> None:
    """C-5 V1：起 uvicorn 托管 engine（消除双进程抢 QMT session）。

    物理意图（spec §3.1 · [[qmt-connect-1-rootcause]] 教训）：
        原 ``_run_forever`` 独立装配 TradingEngine + APScheduler，与生产链
        ``start_all.py → detach uvicorn → main.py lifespan`` 形成两条并存入口。
        两进程 ``gw.connect()`` 抢同一 ``QMT_SESSION_ID`` → QMT 返回 -1（session
        占用）→ 07-29 全天锁死。本函数让 ``python -m trading`` 也走 uvicorn，
        engine 只由 lifespan 装配一次；uvicorn bind 8000 天然单例（第二实例
        ``WSAEADDRINUSE`` → uvicorn exit → 不到 lifespan → 天然不双进程），
        无需文件锁/PID 锁（spec §3.3，A 方案废弃）。

    Why live 不 reload（spec §3.1 R6）：
        ``reload=True`` 时 uvicorn 起 reloader 子进程，子进程会再次 import main →
        lifespan → gw.connect() 抢同一 session（自扰性断线）。live 模式显式
        ``reload=False``；dry_run 开 reload 便利开发热重载（无真网关无抢 session 风险）。

    Why 不再 ``sys.exit`` shadow_gate：
        V1 前独立进程模式下 ``if not check_shadow_gate(): sys.exit(2)`` 是进程级
        决策；V1 后 shadow_gate 检查完全收归 lifespan（main.py:196
        ``if check_shadow_gate(): eng.start()``），server 起不起与 engine 起不起
        解耦——影子期不足时 server 仍起（手动 API 可用），只是不 start scheduler。

    schtasks 兼容（spec §3.4）：
        ``python -m trading`` 命令字面不变（schtasks/PM2/历史注册保持），内部从
        「独立 engine」变「起 uvicorn」——schtasks 触发后起 server，engine 由
        lifespan 装。
    """
    # 惰性 import：避免模块顶层 import uvicorn 拉起 fastapi/starlette 重链
    # （test_main.py 的 import trading.__main__ 不应连带加载 server 栈）。
    import uvicorn
    uvicorn.run(
        "presentation.server.main:app",
        host=os.getenv("SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("SERVER_PORT", "8000")),
        # 显式不传 reuse_port（spec §3.3 R6）：uvicorn 默认 SO_REUSEPORT=False，
        # 第二实例 bind 8000 即 WSAEADDRINUSE exit（端口单例防护）。
        reload=(os.getenv("AUTO_TRADE_MODE", "dry_run") != "live"),
    )


if __name__ == "__main__":
    # C-5 V1：起 uvicorn 托管 engine（替代独立 _run_forever 进程）。
    # mode 仅用于启动日志；shadow_gate 检查收归 lifespan（main.py:196），本块不再
    # sys.exit——server 起不起与 engine 起不起解耦（影子期不足 server 仍起，手动 API 可用）。
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    logger.info("=== 自动交易引擎启动（AUTO_TRADE_MODE=%s，起 uvicorn 托管 engine）===", mode)
    run_server()
