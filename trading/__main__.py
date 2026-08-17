# -*- coding: utf-8 -*-
"""二期自动交易引擎常驻进程入口：``python -m trading``。

============================================================================
定位（C-5 V1 进程模型统一后）
============================================================================
本入口起 **uvicorn server**（``presentation.server.main:app``），engine 由 server
lifespan 装配——与生产 ``start_all.py`` 链**同一入口**，消除历史双进程抢 session
（C-5 V1，[[qmt-connect-1-rootcause]] 教训）。端口 8000 天然单例（第二实例 bind
失败 exit），无需文件锁。``run_server()`` 是薄包装（live 不 reload，防 reloader
子进程抢 session）。（原影子期闸已按 ADR-16 修订移除 · 2026-08-17）

Why engine 可合并进 lifespan（W1 实例属性隔离取代旧进程隔离硬约束）：
- 历史红线「engine 必须独立进程、绝不嵌入 server」是为了防前视污染（engine 注入的
  动态白名单污染 server 手动下单路径）。W1 已把动态白名单从模块级全局
  ``_DYNAMIC`` 改为 engine **实例属性** ``_dynamic_whitelist``；T1 起原模块级活跃引擎
  单例桥被 ``EnginePorts``（``TradingEngine._ports``）显式窄接口取代——pre_open / post_close
  经 ports 注入 gate + 动态白名单读写。server 路径不传 whitelist 即走纯 env 旧路径
  （见 ``engine.py`` 模块 docstring 不变量块）。
- 因此 engine 与 server 同进程不再前视污染，合并进 uvicorn lifespan 安全。

职责切分（薄入口原则 · Karpathy 极简）：
- 本入口只做两件事：① 加载 .env（``load_dotenv(override=True)``）② 调 ``run_server()``
  起 uvicorn。全部业务逻辑（四触发点 cron、APScheduler 装配、交易日判定、影子分流、
  网关 connect/bootstrap）都在 lifespan + ``trading/engine.py::TradingEngine``，本入口
  不重复实现任何业务逻辑。
- （原 ``check_shadow_gate`` 影子期硬闸已按 ADR-16 修订移除 · 2026-08-17——新参数上实盘
  的缓冲由人工 ``risk_ctrl block`` 开关接管，engine 启动不再被自动冻结。）

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
import socket
import sys
from datetime import datetime
from pathlib import Path

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



# ----------------------------------------------------------------------------
# W1.4 单引擎启动探测告警（spec §3.3 · [[qmt-connect-1-rootcause]] 根因收口）
# ----------------------------------------------------------------------------
# 物理意图：08-04 事故真根因是 ``python -m trading`` 嵌套子进程抢 QMT session
# （系统 Python 310 + venv310 父子 37168→35736 并存）。多个 ``python -m trading``
# 并存 → 多写者（CSV/计划/账本）+ QMT session 抢连（connect -1）。
#
# 本探测是**告警手段非根治**（controller 已核实探测局限）：
#   1. Windows 拿不到跨进程 PID：socket connect_ex 只能判端口占用，无法定位持有者 PID
#      （netstat 在测试里不可靠，psutil 非已有依赖——反魔法原则不引入）；
#   2. 嵌套父子拦不住：父子进程共享端口归属父，本探测拦不住嵌套父子——
#      真根治靠运维清理（runbook §1）+ 启动告警；
#   3. 不自动杀进程：探测命中只 ``sys.exit(1)`` 前 CRITICAL + 钉钉告警，绝不 taskkill
#      （误杀 schtasks QuanterServer 拉起的合法链风险高）。
#
# ``_alert_critical`` 别名：测试 monkeypatch 用（钉钉通道软降级 try/except 已兜底，
# 测试再 mock 一层避免触发真实网络/通道装配副作用）。
def _alert_critical(msg: str) -> None:
    """W1.4 启动探测告警通道（thin wrapper，转发 trading.critical._alert_critical）。

    Why 单独包一层而非直接顶层 import：``trading.engine`` 顶层 import 会拉起 apscheduler
    重链（破坏 __main__ 模块加载性能 + 测试隔离），故延迟到函数体内 import 物理真身
    ``trading.critical``（W1-B · Task 10 起不再经 engine re-export 转发）；同时本别名让
    测试可 ``monkeypatch.setattr(__main__, "_alert_critical", fake)`` 单一断口 mock
    （避免 patch critical 内部符号）。
    """
    try:
        # W1-B（Task 10）：改 lazy import 物理真身 trading.critical（engine re-export 垫层
        # 已删）。lazy 保留：避免 __main__ 模块加载期拉起 engine 的 apscheduler 重链。
        from trading.critical import _alert_critical as _engine_alert
        _engine_alert(msg)
    except Exception:
        # 软降级：启动期通道未装/网络异常不阻断 sys.exit 决策（exit 是硬约束，告警是辅助）。
        pass


def _in_testing() -> bool:
    """QUANTER_TESTING=1 → 跳过端口/单实例断言（pytest 不 bind 8000、不抢 session）。"""
    return os.getenv("QUANTER_TESTING") == "1"


def _port_holder_alive(port: int) -> int | None:
    """W1.4：探测 ``port`` 是否被占用；被占用返 -1（PID 未知），空闲返 None。

    实现说明（Karpathy 极简 · 纯标准库 socket）：
        ``socket.connect_ex`` 返 0 表示端口被占（能 connect 上即有监听者）；
        否则（连接拒绝等）返非 0 → 端口空闲。

    ⚠️ 探测局限（docstring 诚实标注，不可假装能拿 PID）：
        - **Windows 拿不到跨进程 PID**：socket API 不暴露持有者 PID；netstat 在测试
          环境不可靠（输出格式/权限波动）；psutil 非已有依赖（反魔法原则不引入）。
          故被占时统一返 ``-1``（语义=「占用但 PID 未知」），由调用方决定告警文案。
        - **嵌套父子拦不住**：父子进程共享端口归属父（父 bind、子继承 fd），
          本探测只能识别「端口被占」无法区分合法父子链 vs 非法双引擎——
          这是告警手段非根治，真根治靠运维清理（runbook §1）。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        if result == 0:
            return -1  # 端口被占，无法跨进程拿 PID（Windows），返 -1 表示「占用但 PID 未知」
        return None
    except OSError:
        # 探测本身异常（权限/网络栈故障）保守视为未占用：避免探测工具自伤启动链
        # （宁可漏报双引擎，也不能误杀唯一合法实例；真双引擎靠 runbook §1 人工核验）。
        return None


def _assert_single_instance(port: int = 8000) -> None:
    """W1.4：单引擎硬约束——``port`` 被既有实例占用 → CRITICAL + 钉钉 + sys.exit(1)。

    物理意图（spec §3.3 · [[qmt-connect-1-rootcause]]）：
        防双 ``python -m trading`` 并存抢 QMT session。端口 8000 是引擎 server 单例锚点
        （uvicorn bind 第二实例本应 WSAEADDRINUSE 自退，但 Windows 嵌套父子 fd 继承
        会绕过 bind 校验——故在 ``run_server`` 入口显式探测兜底）。

    行为：
        - 探测返 None（端口空闲）→ 直接 return，正常起 server；
        - 探测返 -1 或 PID（端口被占）→ ``logger.critical`` + 钉钉 ``_alert_critical``
          + ``sys.exit(1)``（**绝不 taskkill**，误杀 schtasks QuanterServer 链风险高）。

    ⚠️ 局限（同 ``_port_holder_alive`` docstring）：拿不到 PID / 拦不住嵌套父子。
    """
    if _in_testing():
        return
    holder = _port_holder_alive(port)
    if holder is not None:
        msg = (
            f"端口 {port} 已被既有引擎实例占用（holder_pid={holder}）。"
            f"禁止双引擎并行（QMT session 抢连 connect -1 根因，[[qmt-connect-1-rootcause]]）。"
            f"请先按 runbook §1 清理多余 python.exe 进程（tasklist /FI + schtasks QuanterServer 链核对），"
            f"再启动本实例。"
        )
        logger.critical(msg)
        _alert_critical(msg)
        sys.exit(1)



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
        "git=%s started=%s | 口径: eod=next_trading_day, pre_open=today（标的 T+1 对齐）",
        os.environ.get("QMT_SESSION_ID", "?"),
        os.environ.get("QMT_ACCOUNT_ID", "?"),
        os.environ.get("QMT_USERDATA_PATH", "?"),
        os.environ.get("AUTO_TRADE_MODE", "?"),
        os.environ.get("AUTO_CONFIRM_PLAN", "?"),
        _git_rev(),
        datetime.now().isoformat(timespec="seconds"),
    )


def _git_rev() -> str:
    """当前 HEAD 短哈希（P0-3：代码更新后未重启可一眼识别；失败降级 unknown）。"""
    try:
        import subprocess
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[1]),
             "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


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

    schtasks 兼容（spec §3.4）：
        ``python -m trading`` 命令字面不变（schtasks/PM2/历史注册保持），内部从
        「独立 engine」变「起 uvicorn」——schtasks 触发后起 server，engine 由
        lifespan 装。
    """
    # 惰性 import：避免模块顶层 import uvicorn 拉起 fastapi/starlette 重链
    # （test_main.py 的 import trading.__main__ 不应连带加载 server 栈）。
    import uvicorn

    # W1.4 单引擎硬约束探测（spec §3.3）：起 uvicorn 前先确认 port 8000 未被既有
    # 引擎实例占用。命中即 sys.exit(1)（防双进程抢 QMT session，connect -1 根因）。
    # 局限：嵌套父子拦不住（见 _assert_single_instance docstring），真根治靠 runbook §1。
    # A5（P1-2）：生产链 fail-closed——QUANTER_REQUIRE_LIVE=1 时非 live 一律拒绝启动。
    # Why 硬闸：08-05 日志约 20 次 dry_run 实例反复起停（system Python），一旦抢占
    # 8000/日志即「dry_run 接管生产」；生产链（start_server.bat）显式置此 env，
    # 手动/开发入口不置则行为不变（向后兼容）。
    if os.getenv("QUANTER_REQUIRE_LIVE") == "1" and os.getenv("AUTO_TRADE_MODE") != "live":
        logger.critical(
            "生产链要求 AUTO_TRADE_MODE=live，当前=%s，拒绝启动（fail-closed）",
            os.getenv("AUTO_TRADE_MODE"))
        sys.exit(1)
    # DG-G2：live 模式必须配 QUANTER_API_TOKEN——require_write/require_read_cookie 在
    # live 无 token 时 fail-closed（拒所有受保护请求），起这种实例无意义（下单/熔断/SSE
    # 全 401）。Why 启动闸而非运行期才发现：生产部署忘配 token 时，启动即 FATAL 退出，
    # 运维一眼定位；否则进程起来但所有敏感请求 401，故障隐蔽（/health 不触发鉴权故看似存活）。
    # 该闸覆盖**所有**启动入口（start_server.bat / run_trading_engine.bat / 手动 -m trading），
    # 与 start_server.bat 的 bat 级预检互为兜底（bat 预检只能读 bat 进程 env，读不到 .env）。
    if os.getenv("AUTO_TRADE_MODE") == "live" and not os.getenv("QUANTER_API_TOKEN"):
        logger.critical(
            "live 模式未配 QUANTER_API_TOKEN，鉴权 fail-closed 将拒所有受保护请求，拒绝启动（DG-G2）"
        )
        sys.exit(1)
    server_port = int(os.getenv("SERVER_PORT", "8000"))
    if not _in_testing():
        _assert_single_instance(server_port)

    uvicorn.run(
        "presentation.server.main:app",
        # DG-G2：默认 host 127.0.0.1（仅本机回环），防默认监听 0.0.0.0 导致下单/熔断
        # API 裸奔到局域网（同网段任意主机可直连 8000 触发真单）。外网/容器部署须显式
        # 设 SERVER_HOST=0.0.0.0（运维主动知情，而非代码默认）。
        host=os.getenv("SERVER_HOST", "127.0.0.1"),
        port=int(os.getenv("SERVER_PORT", "8000")),
        # 显式不传 reuse_port（spec §3.3 R6）：uvicorn 默认 SO_REUSEPORT=False，
        # 第二实例 bind 8000 即 WSAEADDRINUSE exit（端口单例防护）。
        # QUANTER_DEV_NO_RELOAD=1：dev.py 默认关热重载（防 reloader 子进程僵尸，
        # 见 ops/dev.py 注释）；仅 --reload 时才置 0。live 恒 reload=False（R6）。
        reload=(os.getenv("AUTO_TRADE_MODE", "dry_run") != "live"
                and os.getenv("QUANTER_DEV_NO_RELOAD") != "1"),
    )


if __name__ == "__main__":
    # C-5 V1：起 uvicorn 托管 engine（替代独立 _run_forever 进程）。
    # mode 仅用于启动日志（影子期闸已移除，ADR-16 修订 · 2026-08-17）。
    mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
    logger.info("=== 自动交易引擎启动（AUTO_TRADE_MODE=%s，起 uvicorn 托管 engine）===", mode)
    run_server()
