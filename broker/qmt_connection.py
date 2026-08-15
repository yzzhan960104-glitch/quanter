"""
broker/qmt_connection.py
========================
QmtExecutionGateway 连接层（W2-H1 · broker 四文件分层的契约根）。

接缝说明（为什么在这层）：
- 本模块承载 QMT 网关的【模块契约与运行时家】：xtquant 容错导入、order/account
  11 态字面量、超时/退避/GC 常量、OrderUpdateCallback 签名，以及连接生命周期
  （connect/disconnect/sid 轮换自愈）与 C++ 回调骨架（XtQuantTraderCallback 实现）。
- 为什么常量/辅助函数族的运行时家在这里而不是 broker/qmt.py：单测与运维 patch
  （XtQuantTrader/_CONNECT_TIMEOUT/_cleanup_session_files 等）须指「读取方所在模块」
  才生效（T1 范式：垫片 re-export 副本与真身非同一对象，patch 垫片无效）——连接
  期代码是这些可变全局的几乎全部读取方，故家随读取方落本层；broker/qmt.py 只做
  显式列名 re-export 保外部 import 面零变化。
- 上层文件依赖方向：qmt_io / qmt_business 从本模块 from-import 共享常量（不可变
  值拷贝语义）；类组装在 broker/qmt.py（``QmtExecutionGateway(QmtBusinessMixin,
  QmtIoMixin, QmtConnectionBase)``）。
- 分层红线（spec §5.1）：逻辑只搬位置 + 接缝注释，零行为改动；方法体逐字保留。

底层 API 事实来源：skills/miniqmt/references/xttrader.md（迅投官方），本模块不
臆造任何 xtquant / xtconstant 字段（CLAUDE.md 事实审查红线）。

三条不可逾越红线（继承自原 broker/qmt.py 模块契约，分层后依旧成立）：
1. **同步 C++ ↔ 异步 FastAPI 的线程边界**：xtquant 是同步阻塞的 C++ 绑定，所有
   会阻塞事件循环的调用（start/connect/subscribe）必须经 ``loop.run_in_executor``
   投递到线程池，绝不在协程里直调。
2. **C++ 回调线程 ↔ 主事件循环的状态边界**：XtQuantTraderCallback 的回调运行在
   xtquant 内部 C++ 线程，回调里【只做解析 + call_soon_threadsafe 投递】，绝不
   直接改 FastAPI State、绝不直接 await 钉钉报警——否则轻则竞态，重则跨线程
   持有未完成的协程导致事件循环僵死。
3. **seq ↔ 真实 order_id 的契约边界**：order_stock_async 仅返回请求序号 seq，
   而后续 on_stock_order / on_stock_trade 推送与 cancel_order_stock 用的都是
   柜台真实 order_id；必须以 on_order_stock_async_response 为唯一锚点建映射表，
   否则撤单与回报匹配整体断裂。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable, Mapping, Optional

from broker.base import BaseExecutionGateway
from trading.types.order_state import OrderState  # Layer2 follow-up #4c：改指 types 真身

# ⚠️ 日志名锁定 "broker.qmt"（不用默认 __name__="broker.qmt_connection"）：分层前
# 全部日志经 broker.qmt 一个 logger 出；分层后三文件共用同名 logger，保
# caplog 过滤（test_qmt_alert_degradation_logged 按 logger="broker.qmt" 捕获）与
# 运维日志过滤口径零变化（逻辑只搬红线）。
logger = logging.getLogger("broker.qmt")

# 断线自动重连退避序列（秒，指数退避，B-8）：最多 5 次。
# Why 有上限：无限重连刷爆柜台登录限频；耗尽后保持锁态 + 告警等人工介入。
_RECONNECT_BACKOFFS: tuple[int, ...] = (2, 4, 8, 16, 30)

# 网关调用超时（#9）：connect/submit/cancel 经 run_in_executor 投
# 线程池，须 asyncio.wait_for 兜底防柜台无响应永久阻塞事件循环。固有限制：超时仅放弃等待，
# 底层线程仍在跑（Python 无法真 kill 线程），事件循环不卡死即可。
_CONNECT_TIMEOUT: float = 30.0   # start/connect/subscribe 含网络握手，30s 兜底
_ORDER_TIMEOUT: float = 10.0     # order_stock_async/cancel_order_stock 通常亚秒级，10s 兜底

# 订单流水 GC（#10）：_orders 终态单调增长致内存泄漏，超阈值触发 cleanup_orders。
# 保留近 7 日终态单（对账/审计窗口）+ 全部非终态单（等回报推进），仅删终态且超期者。
_ORDERS_GC_THRESHOLD = 2000
_ORDERS_GC_KEEP_SECONDS = 7 * 86400

# === xtquant 延迟/容错导入 ====================================================
# Why 延迟容错：xtquant 是 Windows + MiniQMT 客户端专属的 C++ 绑定，开发/CI/单测
# 环境通常未安装。用 try/except 退化基类为 object，保证「无 xtquant 也能 import
# 本模块、定义类、跑 Mock」——与项目既有的 MockExecutionGateway 测试体系共存。
try:
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount
    from xtquant import xtconstant  # type: ignore

    _XTQUANT_AVAILABLE = True
except ImportError:  # pragma: no cover - 环境相关，非逻辑分支
    XtQuantTrader = None  # type: ignore[assignment]
    XtQuantTraderCallback = None  # type: ignore[assignment]
    StockAccount = None  # type: ignore[assignment]
    xtconstant = None  # type: ignore[assignment]
    _XTQUANT_AVAILABLE = False

# 网关自身既是 BaseExecutionGateway 子类，又是 XtQuantTraderCallback 实现者
# （网关即回调，register_callback(self) 一步到位）。xtquant 缺失时退化为 object
# 以维持类定义可加载。
_CallbackBase = XtQuantTraderCallback if _XTQUANT_AVAILABLE else object


# === QMT 委托状态整数契约（来源：xttrader.md「委托状态 order_status」表）=========
# Why 用字面量而不用 xtconstant.ORDER_*：
#   1) xtquant 未安装时本模块仍需可 import（见上），枚举名不可硬依赖；
#   2) order_status 字段本身就是 int，柜台返回值是稳定契约，直接比对整数最稳，
#      避免枚举名跨 xtquant 版本重命名导致的映射错乱（实盘状态误判=致命）。
#   连接时会用 _assert_status_contract() 对真实 xtconstant 做一次性一致性校验，
#   防版本漂移，兼顾「显式」与「事实审查」。
_QMT_ORDER_JUNK = 57              # 废单            -> REJECTED
_QMT_ORDER_SUCCEEDED = 56         # 已成            -> FILLED
_QMT_ORDER_PART_SUCC = 55         # 部成            -> PARTIAL_FILLED
_QMT_ORDER_CANCELED = 54          # 已撤            -> CANCELLED
_QMT_ORDER_PART_CANCEL = 53       # 部撤            -> PARTIAL_CANCELLED
_QMT_ORDER_PARTSUCC_CANCEL = 52   # 部成待撤        -> PARTIAL_CANCELLED
_QMT_ORDER_REPORTED_CANCEL = 51   # 已报待撤        -> CANCELLED
_QMT_ORDER_REPORTED = 50          # 已报            -> SUBMITTED
_QMT_ORDER_WAIT_REPORTING = 49    # 待报            -> SUBMITTED
_QMT_ORDER_UNREPORTED = 48        # 未报            -> SUBMITTED
_QMT_ORDER_UNKNOWN = 255          # 未知            -> SUBMITTED（不冒进终态）

# === QMT 账号状态整数契约（来源：xttrader.md「账号状态 account_status」表）=========
# Why 字面量不用 xtconstant.ACCOUNT_STATUS_*：同 order_status，防 xtquant 版本漂移 +
# 无 xtquant 环境仍可 import。_assert_status_contract 连接时校验（T6 补全）。
_QMT_ACC_INVALID = -1         # 无效           -> 锁 + 告警
_QMT_ACC_OK = 0               # 正常           -> 清锁
_QMT_ACC_WAITING_LOGIN = 1    # 连接中         -> log
_QMT_ACC_LOGINING = 2         # 登录中         -> log
_QMT_ACC_FAIL = 3             # 登录失败       -> 锁 + 告警
_QMT_ACC_INITING = 4          # 初始化中       -> log
_QMT_ACC_CORRECTING = 5       # 数据刷新校正中 -> log（校正完有新推送）
_QMT_ACC_CLOSED = 6           # 收盘后         -> 不锁（正常）
_QMT_ACC_ASSIS_FAIL = 7       # 穿透副链接断开 -> 锁 + 告警
_QMT_ACC_DISABLE_BYSYS = 8    # 系统停用（密码错误超限）-> 锁 + 告警
_QMT_ACC_DISABLE_BYUSER = 9   # 用户停用       -> 锁 + 告警

# 应触发熔断锁 + 告警的账号状态集合（账号级故障，on_disconnected 捕获不到）
_QMT_ACC_FATAL = frozenset({
    _QMT_ACC_INVALID, _QMT_ACC_FAIL, _QMT_ACC_ASSIS_FAIL,
    _QMT_ACC_DISABLE_BYSYS, _QMT_ACC_DISABLE_BYUSER,
})

# 上层注入的回报回调签名：接收解析后的 dict，返回 Awaitable（由主线程 create_task 调度）
OrderUpdateCallback = Callable[[Mapping[str, Any]], Awaitable[None]]


def _map_qmt_status(status: int) -> OrderState:
    """
    将 QMT 委托状态整数映射为内部 OrderState。

    风控语义（Why 这么归并）：
    - 53/52（部撤/部成待撤）归 PARTIAL_CANCELLED：已部分成交后撤单流程中，敞口
      按「部分成交 + 剩余撤销」处理，不能误判为全成。
    - 51（已报待撤）归 CANCELLED：撤单指令已被柜台受理，对策略层等同于撤单成功
      的「进行态」，避免重复发撤。
    - 255（未知）保守归 SUBMITTED：绝不因未知就把订单冒进成 FILLED/REJECTED，
      未知状态应由上层对账兜底，而非网关层臆断。
    """
    if status == _QMT_ORDER_SUCCEEDED:
        return OrderState.FILLED
    if status == _QMT_ORDER_PART_SUCC:
        return OrderState.PARTIAL_FILLED
    if status == _QMT_ORDER_JUNK:
        return OrderState.REJECTED
    if status in (_QMT_ORDER_CANCELED,):
        # #9：51（已报待撤）不再归 CANCELLED——撤单指令刚受理未到终态，
        # 归 SUBMITTED 等主推/query_orders 推进到真撤（54），避免 unconfirmed 漏报。
        return OrderState.CANCELLED
    if status in (_QMT_ORDER_PART_CANCEL, _QMT_ORDER_PARTSUCC_CANCEL):
        return OrderState.PARTIAL_CANCELLED
    # 48/49/50/255 等未到终态的中间态统一视为「已提交」，等待后续回报推进
    return OrderState.SUBMITTED


def _assert_status_contract() -> None:
    """
    连接时一次性校验 xtconstant 枚举值与模块字面量契约一致（防版本漂移）。

    Why 必要：状态映射错乱在实盘里是最隐蔽的致命 bug——把「废单」误判成「已成」
    会导致策略以为建仓成功而真实敞口为零，反之亦然。xtquant 升级若改了枚举值，
    这里的强校验会在 connect 阶段直接 fail-fast，而非上线后慢性中毒。

    校验范围（T6 补全）：
    - order 11 态全量（原 7 态 + PARTSUCC_CANCEL/REPORTED_CANCEL/WAIT_REPORTING/UNKNOWN），
      任一漂移会让 _map_qmt_status 状态映射错乱（致命）；
    - account 11 态全量（T1 新增 _QMT_ACC_*），任一漂移会让 on_account_status 误锁/漏锁
      网关（DISABLEBYSYS 漂移=该熔断不熔断，致命程度同 order）。

    Why ACC 同款校验：T1 加字面量时仅加了内部映射，未纳入连接期一致性校验，与 order
    同受 xtquant 版本漂移风险；对称补全后，order 与 acc 字面量漂移均在 connect 阶段
    fail-fast，不留单边盲区。

    命名差异说明（xtconstant 真实命名，非笔误，Why 显式注释防误改）：
    - ACCOUNT_STATUSING（无 LOGIN 后缀）↔ _QMT_ACC_LOGINING；
    - ACCOUNT_STATUS_DISABLEBYSYS（无下划线 BYSYS）↔ _QMT_ACC_DISABLE_BYSYS；
    - ACCOUNT_STATUS_DISABLEBYUSER ↔ _QMT_ACC_DISABLE_BYUSER。
    """
    if not _XTQUANT_AVAILABLE:
        return  # 无 xtquant 时无对象可校验，由 _ensure_xtquant 在连接处拦
    # --- order 11 态契约（订单状态映射锚点）---
    expected = {
        "ORDER_JUNK": _QMT_ORDER_JUNK,
        "ORDER_SUCCEEDED": _QMT_ORDER_SUCCEEDED,
        "ORDER_PART_SUCC": _QMT_ORDER_PART_SUCC,
        "ORDER_CANCELED": _QMT_ORDER_CANCELED,
        "ORDER_PART_CANCEL": _QMT_ORDER_PART_CANCEL,
        "ORDER_PARTSUCC_CANCEL": _QMT_ORDER_PARTSUCC_CANCEL,
        "ORDER_REPORTED_CANCEL": _QMT_ORDER_REPORTED_CANCEL,
        "ORDER_REPORTED": _QMT_ORDER_REPORTED,
        "ORDER_WAIT_REPORTING": _QMT_ORDER_WAIT_REPORTING,
        "ORDER_UNREPORTED": _QMT_ORDER_UNREPORTED,
        "ORDER_UNKNOWN": _QMT_ORDER_UNKNOWN,
    }
    # --- account 11 态契约（账号状态熔断锚点，T1 新增字面量对称校验）---
    expected_acc = {
        "ACCOUNT_STATUS_INVALID": _QMT_ACC_INVALID,
        "ACCOUNT_STATUS_OK": _QMT_ACC_OK,
        "ACCOUNT_STATUS_WAITING_LOGIN": _QMT_ACC_WAITING_LOGIN,
        "ACCOUNT_STATUSING": _QMT_ACC_LOGINING,  # xtconstant 无 LOGIN 后缀，是 STATUSING
        "ACCOUNT_STATUS_FAIL": _QMT_ACC_FAIL,
        "ACCOUNT_STATUS_INITING": _QMT_ACC_INITING,
        "ACCOUNT_STATUS_CORRECTING": _QMT_ACC_CORRECTING,
        "ACCOUNT_STATUS_CLOSED": _QMT_ACC_CLOSED,
        "ACCOUNT_STATUS_ASSIS_FAIL": _QMT_ACC_ASSIS_FAIL,
        "ACCOUNT_STATUS_DISABLEBYSYS": _QMT_ACC_DISABLE_BYSYS,  # xtconstant 无下划线 BYSYS
        "ACCOUNT_STATUS_DISABLEBYUSER": _QMT_ACC_DISABLE_BYUSER,
    }
    # 同款漂移检测：getattr 取真实 xtconstant 值，缺失（None）跳过、存在但 ≠ 字面量 → 漂移
    drifted = [f"{n}={getattr(xtconstant, n)}≠{v}" for n, v in expected.items()
               if getattr(xtconstant, n, None) is not None and getattr(xtconstant, n) != v]
    drifted_acc = [f"{n}={getattr(xtconstant, n)}≠{v}" for n, v in expected_acc.items()
                   if getattr(xtconstant, n, None) is not None and getattr(xtconstant, n) != v]
    if drifted or drifted_acc:
        raise RuntimeError(
            f"xtconstant 枚举契约漂移：order={drifted} acc={drifted_acc}，"
            f"请核对 xttrader.md 后更新本模块"
        )


def _alert_account_status(gw, status_int: int, level: str) -> None:
    """主线程：账号状态告警（fire_and_forget 跨线程安全，链路异常吞不影响主路径）。

    与 _on_disconnect_fatal 同通道，复用 infra.notifier（infra.notifier 别名垫片）。
    """
    try:
        from infra.notifier import NotificationManager, fire_and_forget
        fire_and_forget(NotificationManager.get_default().notify_risk_event(
            f"QMT 账号状态异常 status={status_int} account={gw._account_id}，网关已锁定", level))
    except Exception:
        # G7 告警可观测：告警通道软降级（钉钉网络故障/get_default 异常）不阻断主路径，
        # 但原 except:pass 零日志 → 「监控监控器」盲区（告警系统自己死掉无人知晓）。
        # 加 debug（含 exc_info）让告警通道失效可观测（控制流不变，仍吞异常）。
        logger.debug("告警通道软降级：账号状态告警发送失败 status=%s account=%s",
                     status_int, getattr(gw, "_account_id", "?"), exc_info=True)


def _stop_trader_safely(trader) -> None:
    """停掉 XtQuantTrader 实例（释放 sid 会话）；异常吞掉不阻断。

    2026-08-03 根治：同 sid 的 XtQuantTrader 未 stop 即被替换/进程退出，会话会
    残留共享内存（down_queue_win_{sid} 等队列文件），此后任何同 sid connect 必返
    -1——包括本进程重试（旧实例未停，新实例自锁）。connect 前必须停旧实例。
    """
    if trader is None:
        return
    try:
        trader.stop()
    except Exception:
        logger.warning("QMT trader.stop() 异常（已忽略，由 connect -1 清理兜底）",
                       exc_info=True)


def _cleanup_session_files(userdata_path: str, session_id: int) -> list[str]:
    """删除指定 sid 的残留会话队列文件（仅本 sid，不动客户端 xtmodel/xtquant 队列）。

    物理依据（2026-08-03 系统性实验）：进程未调 stop() 退出（强杀/崩溃）会残留
    down_queue_win_{sid} 会话文件，同 sid 后续 connect 恒返 -1；清掉即恢复。
    只按 sid 精确匹配（down_queue_win_{sid} / lock_down_queue_win_{sid}，含 __mutex
    后缀），绝不误删客户端队列（xtmodel/xtquant 命名不同）。删除失败（文件被
    其他进程占用）→ 记录并跳过，由上层决定是否继续重试。

    Returns:
        实际删除的文件名列表（供日志/测试断言）。
    """
    import glob as _glob

    removed: list[str] = []
    if not userdata_path or not os.path.isdir(userdata_path):
        return removed
    down_prefix = f"down_queue_win_{session_id}"
    lock_prefix = f"lock_down_queue_win_{session_id}"
    candidates = (
        _glob.glob(os.path.join(userdata_path, "down_queue_win_*"))
        + _glob.glob(os.path.join(userdata_path, "lock_down_queue_win_*"))
    )
    for f in candidates:
        name = os.path.basename(f)
        base = name.split("__")[0]          # 去 __mutex 后缀再精确比对
        if base != down_prefix and base != lock_prefix:
            continue
        try:
            os.remove(f)
            removed.append(name)
        except OSError:
            logger.warning("清理残留会话文件失败 %s（可能被占用，跳过）", f, exc_info=True)
    return removed


def _client_process_alive() -> bool | None:
    """miniQMT 客户端进程存在性探测（委托 ops.process_topology.client_status，audit_ssot 同口径）。

    返回 True=进程在跑 / False=进程未起 / None=探测失败（保守视为未知，调用方放行原逻辑）。

    Why 进程级而非文件级：is_client_ready 按 08-04 事故教训只查目录（connect 返回码唯一
    权威），但 L2 sid 轮换是重操作（100 候选 × 30s 超时 + 每候选建 down_queue 会话文件）
    ——客户端进程不存在时 connect -1 的真实语义是「无客户端可连」而非「sid 被占」，
    轮换 100 个 sid 纯属无效功（2026-08-14 实测：无客户端启动引擎 → 轮换静默推进
    10 候选后中止，userdata 残留 down_queue_win_123501~123510）。轮换前用进程判据
    短路，直进 L3 fail-closed，恢复交给 _health_guard 60s 轮询（客户端进程出现即重连）。
    """
    from ops.process_topology import client_status
    return client_status()["running"]


def _client_activity_age_secs(userdata_path: str) -> float | None:
    """客户端活跃文件最新 mtime 年龄（秒）；无任何活跃信号 → None。

    patterns 与 _client_staleness_diag 同源（quoter 行情刷新是登录/收数据的可靠存活
    信号；miniqmtShm/up_queue 是启动时一次性生成不可靠）。抽函数供轮换闸复用。
    """
    import glob as _glob
    if not userdata_path or not os.path.isdir(userdata_path):
        return None
    now = time.time()
    newest = 0.0
    for pat in ("miniqmtShm*Cache*", "up_queue_win_*", "quoter", "quoter/*"):
        for f in _glob.glob(os.path.join(userdata_path, pat)):
            try:
                m = os.path.getmtime(f)
                if m > newest:
                    newest = m
            except OSError:
                continue
    return None if newest == 0.0 else (now - newest)


def _client_servable(userdata_path: str, staleness_sec: int = 300) -> bool | None:
    """客户端「可服务」判据：进程在跑 ∧ 活跃文件新鲜 → True / False / None(未知)。

    Why 比 _client_process_alive 强（2026-08-14 实测）：客户端进程存在但未登录
    （quoter 缓存 6.7 天未刷新）时任意 sid connect 恒 -1——轮换 100 候选纯属无效功，
    且每候选 start() 预分配 75MB down_queue 文件（实测 2 分钟 ≈2.7GB 磁盘）。轮换
    前置闸用本判据：False → 跳过轮换直进 L3 fail-closed，恢复交给 _health_guard
    （客户端登录后健康检查自动重连）。探测失败/无活跃信号 → None 保守放行原轮换。

    非交易时段语义（评审 2026-08-15 质询，裁定为设计意图而非误伤）：隔夜/周末
    quoter 无刷新，已登录的健康客户端本判据也返 False——即「非交易时段不轮换」。
    这与 L2 自愈不冲突：非交易时段 connect -1 的主因是柜台不可连（轮换本就无效功），
    sid 真被占的场景等交易时段 quoter 刷新后 health_guard 走轮换自愈。保守收紧
    （宁可晚一轮轮换，不烧磁盘不做无效功）与 G 波 fail-closed 哲学同源。
    """
    running = _client_process_alive()
    if running is False:
        return False
    if running is None:
        return None
    age = _client_activity_age_secs(userdata_path)
    if age is None:
        return None
    return age <= staleness_sec


def _drop_candidate_session_files(userdata_path: str, sid: int) -> None:
    """L2 轮换失败候选的会话文件清理（G8：失败不留 down_queue 残留，防 userdata 污染）。

    Why 仅失败路径调用：轮换成功时新 sid 的队列文件是真实会话上下文（连接在用），
    绝不可删；失败候选的文件是死会话残留，下次 connect 前置清理只清 preferred，
    轮换候选文件会永久累积（2026-08-14 实测 123501~123510 残留）。
    """
    try:
        _cleanup_session_files(userdata_path, sid)
    except Exception:
        logger.debug("L2 轮换失败候选 sid=%s 会话文件清理失败（下轮轮换前置清理兜底）",
                     sid, exc_info=True)


def _used_session_ids(userdata_path: str) -> set[int]:
    """扫 down_queue_win_* / lock_*queue_win_* 提取在用 sid（L2 轮换的占用登记表）。

    物理依据（spec §4.4）：userdata 目录就是 sid 占用登记表——同一 sid 同一时刻只能
    被一个进程独占，目录里出现 down_queue_win_{sid} 即该 sid 在用/残留；轮换前必须
    避开，否则新 sid 与其它进程撞车（connect -1 复发）。
    """
    import glob as _glob
    import re as _re

    used: set[int] = set()
    if not userdata_path or not os.path.isdir(userdata_path):
        return used
    for pat in ("down_queue_win_*", "lock_*queue_win_*"):
        for f in _glob.glob(os.path.join(userdata_path, pat)):
            m = _re.search(r"(\d+)\s*$", os.path.basename(f).split("__")[0])
            if m:
                used.add(int(m.group(1)))
    return used


def _candidate_session_ids(preferred: int, used: set[int], limit: int = 100) -> list[int]:
    """preferred 起有界递增找未占用 sid（裁定 L1=100；同段递增=不跨账号区间）。"""
    return [preferred + i for i in range(1, limit + 1) if (preferred + i) not in used]


def _write_runtime_session(preferred: int, actual: int,
                           account_id: str | None = None) -> None:
    """L2 轮换落地：写 M2 真相源（DB account.session_id）+ 运行态快照（json）。

    M2 降级语义（2026-08-15 tech-debt · actual_sid 单 SSoT）：实际 sid 的唯一真相源是
    DB ``account.session_id``；``logs/engine_session.json`` 只是运行态快照（非真相源，
    仅供人眼 cat——消费端 supervisor/ops 一律经 ``state_store.get_session_id`` 读 DB）。
    Why 轮换点直写 DB（补 B2 计划欠账）：旧实现只在 json 写轮换结果、DB 靠 engine
    bootstrap L3 回写——盘中轮换后 DB 滞后到下次重启才对齐，supervisor 改读 DB 即
    观测失真；本函数两写口同点落，读口永远新鲜。

    Why 不动 .env（spec §4.4）：preferred 是引擎身份/锁键/观测锚点，轮换只记录实际
    值——.env 仍是人工期望值，两个值同时在观测端点展示，漂移可见但不阻断。
    Why DB 写失败不阻断：连接本身已成功，观测缺值不是交易红线（与 json 同语义）。
    Why state_store 函数内 lazy import：broker 是执行叶子包，顶部 import trading
    state_store 会在「broker 首加载序」重新织入 import 环（T14 刚断的病）；调用点
    一定在全模块加载完成后（轮换只发生在运行期），lazy 零环风险。
    """
    import json as _json
    from datetime import datetime as _dt
    from pathlib import Path as _Path
    # ① json 运行态快照（降级：非真相源，note 字段自述防误用）
    try:
        p = _Path(__file__).resolve().parent.parent / "logs" / "engine_session.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            _json.dumps({"preferred": preferred, "actual": actual,
                         "rotated_at": _dt.now().isoformat(timespec="seconds"),
                         "note": "运行态快照·非真相源（真相源=state_store.account.session_id）"},
                        ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        logger.warning("L2 runtime 快照写入失败（不阻断连接）", exc_info=True)
    # ② M2 真相源：DB account.session_id（列级精准 UPDATE，不抹 mode/userdata_path）
    if account_id:
        try:
            from trading import state_store as _ss
            _ss.set_session_id(account_id, int(actual))
        except Exception:
            logger.warning("M2 DB 真相源写入失败（不阻断连接，bootstrap L3 兜底）",
                           exc_info=True)


class QmtConnectionBase(BaseExecutionGateway, _CallbackBase):  # type: ignore[misc]
    """
    QmtExecutionGateway 连接层基类（W2-H1 分层 · 逻辑只搬）。

    职责边界（为什么在这层）：
    - 连接生命周期：connect/disconnect/_run_bootstrap/_try_rotate_session（L2 sid
      轮换自愈）与客户端就绪探测（is_client_ready/_client_staleness_diag）；
    - C++ 回调骨架：XtQuantTraderCallback 的 8 个 on_* 回调（运行在 xtquant 内部
      C++ 线程，只做「解析 + call_soon_threadsafe 投递」）；
    - 共享状态的唯一初始化点：``__init__`` 在本层定义 self._loop/_trader/_account/
      _lock_down/_risk_halted/_main_push_available/_seq_to_real/_seq_to_client/
      _orders/_on_order_update/_reconnecting，供 QmtIoMixin/QmtBusinessMixin（组装于
      broker/qmt.py 的 QmtExecutionGateway）经 self 共享——mixin 间零新通信机制。

    IO 六方法在 broker/qmt_io.py（QmtIoMixin）；下单/撤单/锁风控状态机/回报处理在
    broker/qmt_business.py（QmtBusinessMixin）。类组装：
    ``class QmtExecutionGateway(QmtBusinessMixin, QmtIoMixin, QmtConnectionBase)``。

    MiniQMT 实盘执行网关。

    线程模型（务必读懂再改）：
    - 主事件循环线程：connect/submit_order/cancel_order/_fetch_broker_positions 与
      _process_order_update / _on_disconnect_fatal 均在此线程跑。
    - xtquant C++ 回调线程：on_* 系列回调在此线程触发，只做「解析 + 投递」。
    - 线程池（默认 ThreadPoolExecutor）：承载 start/connect/subscribe/query/order
      等同步阻塞调用，避免它们卡死事件循环。

    断线保护：on_disconnected 触发后立即原子置位 _lock_down，submit_order 据此
    熔断拒单，杜绝断线窗口期内的废单重发（CLAUDE.md 状态机边界红线）。
    """

    # ------------------------------------------------------------------ 构造
    def __init__(
        self,
        userdata_path: Optional[str] = None,
        account_id: Optional[str] = None,
        session_id: Optional[int] = None,
        strategy_name: Optional[str] = None,
    ) -> None:
        """
        Args:
            userdata_path: MiniQMT 客户端 userdata_mini 完整路径；None 则读
                环境变量 QMT_USERDATA_PATH。
            account_id: 资金账号；None 则读 QMT_ACCOUNT_ID。
            session_id: 会话编号，不同 Python 策略进程必须不同（xttrader.md
                创建API实例备注）；None 则读 QMT_SESSION_ID，缺省 123456。
            strategy_name: 下单 strategy_name 字段，缺省 "quanter"，用于 QMT
                端策略归类与回报对账。

        Raises:
            ValueError: userdata_path / account_id 既未传参也无环境变量。
        """
        self._userdata_path: str = userdata_path or os.environ.get("QMT_USERDATA_PATH", "")
        self._account_id: str = account_id or os.environ.get("QMT_ACCOUNT_ID", "")
        if not self._userdata_path:
            raise ValueError(
                "缺少 QMT 用户数据目录：请设置环境变量 QMT_USERDATA_PATH，"
                "或在构造 QmtExecutionGateway 时传入 userdata_path"
            )
        if not self._account_id:
            raise ValueError(
                "缺少 QMT 资金账号：请设置环境变量 QMT_ACCOUNT_ID，"
                "或在构造 QmtExecutionGateway 时传入 account_id"
            )
        self._session_id: int = session_id or int(os.environ.get("QMT_SESSION_ID", "123456"))
        self._strategy_name: str = strategy_name or os.environ.get("QMT_STRATEGY_NAME", "quanter")

        # 运行态：连接成功前 _loop=None，submit_order 访问会显式失败而非静默误用
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._trader: Any = None          # XtQuantTrader 实例（Any：xtquant 缺失时为 None）
        self._account: Any = None         # StockAccount 实例
        self._connected: bool = False
        # 断线锁定：初始 False（未连接由下方 _connected 检查兜底拒单，避免初始态被
        # get_status 误判为 vetoed_by_risk）；connect 成功保持 False；
        # on_disconnected/emergency_halt 置 True，风控层据此熔断。
        self._lock_down: bool = False
        # 风控熔断粘滞标志（#6）：emergency_halt/日内-3% 熔断置 True，
        # health_guard/账号 OK 均不得自动解除；解锁必须显式 clear_risk_halt()。
        self._risk_halted: bool = False

        # 主推可用性标志（T5）：subscribe 成功保持 True，失败置 False（订单状态靠主动查询兜底）。
        # Why 单列：subscribe 失败时 connect 仍可能成功（socket 通），但拿不到
        # on_stock_order 主推，订单状态进入「盲区」——上层 engine 须靠本标志在触发点
        # 前（pre_open/stop_loss_monitor 等）调 _sync_orders_if_stale 主动 query_orders
        # 补全 _orders。若不单列而只 warning，上层无法区分「主推正常」与「需兜底」，
        # 惰性同步会误触（撞柜台限频）或漏触（盲区持久化）。
        self._main_push_available: bool = True

        # seq ↔ 真实 order_id ↔ 客户端单号 的三向映射（撤单与回报匹配的唯一依据）
        self._seq_to_real: dict[int, int] = {}     # seq -> QMT 柜台真实 order_id
        self._seq_to_client: dict[int, str] = {}   # seq -> 调用方透传的客户端单号

        # 订单回报流水：主线程独占读写，供上层 query 与对账（call_soon_threadsafe 保证）
        self._orders: dict[str, dict[str, Any]] = {}

        # 上层注入的异步回报回调（钉钉报警 / State 持久化），主线程 create_task 调度
        self._on_order_update: Optional[OrderUpdateCallback] = None

        # M1：重连互斥标志——on_disconnected→_reconnect 与 T8 健康守护 job 两条重连路径
        # 共用入口的互斥锁。Why：两条路径并发触发时会同时 start/connect 同一 session_id，
        # QMT 对已占用的 sid 返回 -1（connect 失败），形成「自扰性断线」死循环。
        # 用 bool 做软锁：先到者置 True，后到者见 True 直接 return 让出，finally 释放。
        self._reconnecting: bool = False

    # ------------------------------------------------------------------ 连接
    def is_client_ready(self, staleness_sec: int = 300) -> bool:
        """探测 miniQMT 客户端是否就绪（W1.1 · 2026-08-04 根治后二次重定义）。

        判据（connect 返回码唯一权威原则）：
            userdata 目录存在且非空 → True（客户端进程可能在跑，放行让上层 connect，
            由 trader.connect() 返回码定权威结论：0=成功 / -1=session 残留自愈 / 其他=环境故障）。
            目录缺失/为空 → False（客户端必然未启动，connect 必失败，唯一该挡的场景）。

        ⚠️ 不再用 miniqmtShm*Cache*/up_queue_win_* 的 mtime 做硬前置：
            那是客户端【启动时一次性生成】的共享内存镜像，运行期间不刷新，>5min 即判死 →
            _health_guard 永不 connect（08-04 事故根因：09:22 pre_open 静默跳过、计划正常
            却一张单没挂）。mtime 降级为 _client_staleness_diag 的日志分类素材，仅供
            health_guard WARNING 文案用，绝不阻断 connect 尝试（否则换探针复发静默跳过）。

        Args:
            staleness_sec: 保留入参兼容既有 caller（_gw_health_gate/bootstrap/_health_guard
                均以默认 300s 调用），本方法已不做 mtime 判据，入参仅透传给诊断函数。

        Why 纯文件检查：不触达 xtquant（C++ 扩展），CI/单测/无 SDK 环境可安全调用。
        """
        if not self._userdata_path or not os.path.isdir(self._userdata_path):
            return False
        # 目录存在但完全空（刚创建未登录）也视未就绪——connect 必失败（无会话上下文）。
        # Why 不放行空目录：避免空跑 connect 撞柜台，与「目录缺失」等价挡掉。
        try:
            if not any(os.scandir(self._userdata_path)):
                return False
        except OSError:
            # 目录不可读（权限错/IO 错）保守视未就绪，让上层走诊断文案定位
            return False
        return True

    def _client_staleness_diag(self, staleness_sec: int = 300) -> str:
        """客户端活跃度诊断文案（W1.1，仅供 health_guard WARNING 用，不做硬前置）。

        物理：is_client_ready 已不做 mtime 判据（connect 返回码唯一权威），但运维仍需
        一眼定位「断线是客户端未起 / 未登录 / 仅仅是 shm 陈旧」。本函数把原 mtime 启发式
        改造成纯日志分类素材，返回稳定文案供 T2 _health_guard WARNING 拼接。

        Returns:
            四态稳定文案（T2 会断言含「不存在/目录空/无活跃文件/陈旧/正常」之一）：
              - 目录缺失 → "userdata 目录不存在（客户端未安装/路径错）"
              - 目录空   → "userdata 目录空（客户端未登录）"
              - 目录不可读 → "userdata 目录不可读"
              - 无活跃文件（仅目录存在）→ "无活跃文件（仅目录存在，客户端可能未登录）"
              - 活跃文件陈旧 → "文件最新 mtime 陈旧 N 分钟"
              - 活跃文件新鲜 → "正常（文件新鲜）"
        """
        import glob as _glob
        if not self._userdata_path or not os.path.isdir(self._userdata_path):
            return "userdata 目录不存在（客户端未安装/路径错）"
        try:
            if not any(os.scandir(self._userdata_path)):
                return "userdata 目录空（客户端未登录）"
        except OSError:
            return "userdata 目录不可读"
        # 活跃度启发式：quoter 行情目录 + 启动缓存任一新鲜 → 活跃；全老旧 → 陈旧告警。
        # Why 加 quoter：miniqmtShm*/up_queue_* 是启动时一次性生成（不刷新），quoter
        #    行情目录在盘中会被行情主推刷新，是更敏感的存活信号。
        # Fix3（用户两轴 review · Windows mtime 失效）："quoter" 只匹配目录本身，Windows
        #    目录 mtime 仅在内部文件增删时刷新（行情主推覆盖写已有文件不动目录 mtime），
        #    一天只变一次 → 客户端正常收行情时仍误报陈旧。加 "quoter/*" glob 到文件级，
        #    行情刷新文件 mtime 即更新，是 Windows 下唯一可靠的存活信号。
        # 扫描逻辑抽到 _client_activity_age_secs（轮换闸 _client_servable 复用同源判据）。
        age = _client_activity_age_secs(self._userdata_path)
        if age is None:
            # 目录非空但活跃 patterns 都没匹配（可能只有 down_queue 等引擎文件）
            return "无活跃文件（仅目录存在，客户端可能未登录）"
        age_min = int(age / 60)
        # staleness_sec//60 把秒阈值换算成分钟阈值，与 mtime age_min 同口径比对
        return f"文件最新 mtime 陈旧 {age_min} 分钟" if age_min > staleness_sec // 60 else "正常（文件新鲜）"

    async def _run_bootstrap(self, sid: int) -> tuple[int, int]:
        """构造 XtQuantTrader(sid) → start/connect/subscribe（L2 参数化 sid 共用）。

        Why 抽方法：connect 的首选两轮重试与 L2 sid 轮换都要走同一段
        start/connect/subscribe 时序（含异常兜底），复制两份会漂移。
        """
        trader = XtQuantTrader(self._userdata_path, sid)
        trader.register_callback(self)
        self._trader = trader          # 失败路径也要能 stop 到本次实例
        self._account = StockAccount(self._account_id)

        def _bootstrap() -> tuple[int, int]:
            trader.start()
            rc = trader.connect()
            if rc != 0:
                # 连接失败时不必 subscribe，直接返回，由外层判定
                return rc, -1
            sub = trader.subscribe(self._account)
            return rc, sub

        try:
            # #9：wait_for 兜底防柜台无响应永久阻塞事件循环
            # （超时→ConnectionError→_reconnect）。
            return await asyncio.wait_for(
                self._loop.run_in_executor(None, _bootstrap),
                timeout=_CONNECT_TIMEOUT)
        except Exception as exc:
            # 失败也要停掉本次实例（释放 sid），再置锁抛出
            _stop_trader_safely(self._trader)
            self._trader = None
            self._lock_down = True
            raise ConnectionError(
                f"QMT connect 异常/超时(>{_CONNECT_TIMEOUT}s)：{exc}") from exc

    async def _try_rotate_session(self) -> int | None:
        """L2：preferred -1 后自动轮换未占用 sid；成功返 sub_rc，失败返 None。

        物理意图（spec §4.4 · 裁定 L1-L4）：connect -1 = 该 sid 被占/残留，清理两轮
        后仍失败 → 从 preferred 起有界找未占用 sid（上限 100）自动重连，成功把实际
        sid 写入 M2 真相源（DB account.session_id）+ json 运行态快照，.env preferred
        不变。轮换耗尽返 None，由 connect 走既有 L3 失败路径（fail-closed + 告警文案）。
        """
        preferred = self._session_id
        used = _used_session_ids(self._userdata_path)
        candidates = _candidate_session_ids(preferred, used, limit=100)
        if not candidates:
            logger.warning("L2 无可用 sid（preferred=%s used=%s）→ L3 人工兜底",
                           preferred, sorted(used))
            return None
        for candidate in candidates:
            try:
                _cleanup_session_files(self._userdata_path, candidate)
            except Exception:
                pass
            try:
                rc2, sub2 = await self._run_bootstrap(candidate)
            except ConnectionError:
                # G8：每候选 30s 超时——逐轮日志让轮换进度可观测（否则无声推进最坏
                # ~50 分钟被误判「挂死」）；失败候选不留会话文件（防 userdata 污染累积）。
                logger.warning("L2 轮换候选 sid=%s connect 超时（>%ss），换下一候选",
                               candidate, _CONNECT_TIMEOUT)
                _drop_candidate_session_files(self._userdata_path, candidate)
                continue
            if rc2 == 0:
                self._session_id = candidate
                # M2：account_id 必须透传——轮换点直写 DB 真相源（见 writer docstring）
                _write_runtime_session(preferred, candidate,
                                       account_id=self._account_id)
                logger.warning(
                    "L2 sid 自动轮换 preferred=%s → actual=%s（connect -1 自愈）",
                    preferred, candidate)
                return sub2
            _stop_trader_safely(self._trader)
            self._trader = None
            logger.debug("L2 轮换候选 sid=%s connect 返 %s，换下一候选", candidate, rc2)
            _drop_candidate_session_files(self._userdata_path, candidate)
        logger.warning("L2 sid 轮换耗尽（preferred=%s 前 5 候选=%s）→ L3 人工兜底",
                       preferred, candidates[:5])
        return None

    async def connect(self) -> None:
        """
        建立并保活 QMT 连接（BaseExecutionGateway.connect 实现）。

        时序严格遵循 xttrader.md「快速入门」：
            XtQuantTrader(path, sid) -> register_callback(self) -> start()
            -> connect() [==0] -> StockAccount -> subscribe() [==0]

        Why self 即 callback：本类继承 XtQuantTraderCallback，register_callback(self)
        一步完成回调注册，避免再造一个内部 callback 类增加跨对象状态同步。

        Why 全程 run_in_executor：start/connect/subscribe 均为同步阻塞的 C++ 调用，
        直调会卡住 FastAPI 事件循环（连带拖垮所有其他协程，包括行情与心跳）。
        用一个闭包 _bootstrap 把三步串成一次线程池任务，减少跨线程往返。

        2026-08-03 根治（-1 自锁）：
            - **stop-before-recreate**：重连前必须先 stop 旧 XtQuantTrader 实例。
              系统性实验证实：同 sid 的旧实例未 stop 即被替换，会话残留共享内存，
              新实例 connect 恒返 -1（同进程自锁）。
            - **-1 自愈重试**：connect 返 -1 = 死进程残留会话（强杀/崩溃未 stop），
              清掉本 sid 队列文件后重试一次，无需人工换 sid / 删文件。
        """
        self._loop = asyncio.get_running_loop()
        self._ensure_xtquant()
        _assert_status_contract()

        # 1. stop-before-recreate：旧实例未 stop 会继续占用 sid（同进程重试自锁 -1）
        if self._trader is not None:
            _stop_trader_safely(self._trader)
            self._trader = None

        # W1.3（08-04 根治）：connect 前预防性清本 sid 残留队列，不等 -1 补救。
        # Why 前置（区别于下方 attempt 内兜底）：事故机 userdata_mini\down_queue_win_123459
        # 残留 75MB 旧成交回报队列。connect 连上瞬间客户端会重放旧队列 → 历史 24 笔
        # 成交回报再推一遍 → CSV 重复（W3 幂等是第二道防线，这里是第一道）。即便本次
        # connect 首轮就返 0（不走 -1 补救），残留队列仍会被客户端在 subscribe 后立即
        # 推送——故必须在「构造 XtQuantTrader 之前」清掉，attempt 内的 -1 兜底来不及。
        # Why 软降级：清理是防患于未然的第一道防线，但非 connect 的必要条件——即便
        # 残留没清掉 connect 仍可能成功（柜台未必每次重放）；若清理异常直接抛，反而
        # 把「可恢复的文件系统问题」升级成「connect 全失败」，违反 fail-safe。attempt
        # 循环内的 -1 兜底是第二道防线。只清 down_queue_win_{sid}（引擎自有会话文件），
        # 不动 xtquant/xtmodel 队列（_cleanup_session_files 按 sid 精确匹配）。
        try:
            _pre_cleaned = _cleanup_session_files(self._userdata_path, self._session_id)
            if _pre_cleaned:
                logger.info("QMT connect 前置清理本 sid 残留队列：%s", _pre_cleaned)
        except Exception:
            logger.warning("QMT connect 前置清理异常（忽略，继续 attempt）", exc_info=True)

        # 2. 首选 sid 最多两轮：首轮失败（-1=残留会话）→ 清本 sid 队列文件 → 重试一轮
        connect_rc: int | None = None
        sub_rc: int = -1
        sid = self._session_id
        for attempt in (1, 2):
            try:
                connect_rc, sub_rc = await self._run_bootstrap(sid)
            except ConnectionError:
                raise

            if connect_rc == 0:
                break                        # 连接成功

            # T11（2026-08-15）：connect **每次**返 -1 都留痕（Notes 兑现：
            # memory 688160 拒因黑盒教训——rc=-1 柜台无文本回报，不留痕则下方 L1 清理
            # 重试 / L2 sid 轮换 / L3 fail-closed 整条兜底链只见结果不见过程，事后无法
            # 归因「-1 发生在第几轮、当时 sid 是谁」）。rc 直接打字面量：-1 不是委托
            # 状态码，硬塞 _map_qmt_status 只会落默认分支打成误导性的 status=SUBMITTED
            # （rc 与 OrderState 是两套语义，混叠比缺字段更危险）；msg 记 attempt 轮次
            # + -1 物理含义。
            # ⚠️ 只加观测，不改重试次数/顺序（G8 刚重构，周一实战前零行为变更）。
            if connect_rc == -1:
                logger.warning(
                    "connect -1 sid=%s rc=%s msg=%s",
                    sid, connect_rc,
                    f"attempt={attempt}/2：死进程残留会话/sid 被占（柜台无文本回报）")

            # 连接失败：立即停掉本次实例释放 sid（防止同进程下次重试自锁）
            _stop_trader_safely(self._trader)
            self._trader = None
            if connect_rc == -1 and attempt == 1:
                # -1 = 死进程残留会话（强杀/崩溃未 stop）→ 清本 sid 队列文件后重试
                cleaned = _cleanup_session_files(self._userdata_path, sid)
                logger.warning(
                    "QMT connect -1（session 残留）：已停旧实例并清理 %s，重试第 2 次",
                    cleaned or "无残留文件")
                continue
            break

        # L2（spec §4.4 · 裁定 L1-L4）：-1 清理两轮后仍失败 → 自动轮换未占用 sid。
        # 单实例锁仍以 preferred 为键（引擎身份），轮换只改 trader 会话 sid。
        if connect_rc == -1:
            # run_in_executor 投递（模块契约：协程内绝不直调同步阻塞调用）——
            # _client_servable 链路 client_status 是 subprocess.run(powershell,
            # timeout=8)（进程启动常耗 0.5-2s），直调会卡事件循环拖垮行情/心跳协程
            # （评审 2026-08-15 抓出的唯一破例，与本模块 _run_bootstrap 同纪律）。
            _servable = await self._loop.run_in_executor(
                None, _client_servable, self._userdata_path)
            if _servable is False:
                # G8（2026-08-14 实测）：客户端不可服务（进程缺失 / 进程在但未登录，
                # quoter 缓存数天未刷新）时 -1 的真实语义是「客户端未就绪」而非
                # 「sid 被占」——轮换 100 候选纯属无效功（每候选 30s 超时 + start()
                # 预分配 75MB down_queue 文件，实测 2 分钟 ≈2.7GB 磁盘 + 最长 ~50 分钟
                # 「挂死」）。跳过轮换直进 L3 fail-closed，恢复交给 _health_guard 60s
                # 轮询（客户端就绪后自动重连）。探测失败/无信号(None)保守放行原轮换。
                logger.warning(
                    "QMT 客户端不可服务（进程缺失或活跃文件陈旧）——跳过 L2 sid 轮换"
                    "（-1=客户端未就绪而非被占），直进 L3；health_guard 将轮询恢复")
            else:
                _rot_sub = await self._try_rotate_session()
                if _rot_sub is not None:
                    connect_rc, sub_rc = 0, _rot_sub

        if connect_rc != 0:
            # connect 返回非 0 即连接失败（xttrader.md：返回 0 表示成功）
            self._lock_down = True
            # M1：按返回码区分失败原因，供 M4 钉钉告警精准定位（-1 与其他码处置路径不同）
            # -1 的物理含义：session 被占用（残留锁 / 他进程已用同一 sid 连上 MiniQMT）。
            # 其他非零码多为环境类故障（客户端未启动 / userdata 路径错 / 版本不匹配）。
            reason = (
                "session 疑似被占用（残留锁/他进程占用 sid，自动清理后仍失败）"
                if connect_rc == -1 else f"返回码 {connect_rc}"
            )
            raise ConnectionError(
                f"QMT connect 失败（{reason}）；userdata={self._userdata_path}。"
                f"若 -1 且客户端在跑，跑 scripts/qmt_clear_session_lock.py 清残留或换 sid"
            )
        if sub_rc != 0:
            # subscribe 失败不致命但危险：拿不到主推回报（on_stock_order/on_stock_trade），
            # 订单状态进入盲区。置 _main_push_available=False 让上层在触发点前靠
            # _sync_orders_if_stale 主动 query_orders 兜底（T5 决策：不引入后台轮询，
            # 只在触发点前惰性补全，避免新调度复杂度 + 撞柜台限频）。
            self._main_push_available = False
            logger.warning(
                "QMT subscribe 返回 %s（0=成功，-1=失败），委托/成交主推缺失，"
                "订单状态将退化为主动查询模式（_sync_orders_if_stale 触发点前补全）", sub_rc
            )
        else:
            # subscribe 成功：主推正常（含重连成功后重新 subscribe 的恢复路径）。
            self._main_push_available = True

        self._connected = True
        if not self._risk_halted:
            self._lock_down = False  # 仅网络断线重连清锁；风控熔断(_risk_halted)粘滞不清
        logger.info("QMT 网关已连接 account=%s session=%s", self._account_id, self._session_id)

    async def disconnect(self) -> None:
        """优雅断开：stop() 同步阻塞，投线程池；无条件回锁防断开瞬间的发单竞态。"""
        if self._trader is not None and self._loop is not None:
            await self._loop.run_in_executor(None, self._trader.stop)
        self._trader = None    # 2026-08-03：断开即释放引用，防 connect 重复 stop 已停实例
        self._connected = False
        self._lock_down = True
        logger.info("QMT 网关已断开 account=%s", self._account_id)

    # ================================================ XtQuantTraderCallback
    # 以下回调全部运行在 xtquant 的 C++ 线程！
    # 铁律：只做 try-except 包裹的解析 + call_soon_threadsafe 投递，零阻塞、零
    #       异步副作用、零对 self._orders 的直接写（写操作只能发生在主线程）。
    # =====================================================================

    def on_disconnected(self) -> None:
        """
        连接断开（C++ 线程）。

        Why 先原子置 _lock_down 再投递：submit_order 在主线程并发读 _lock_down，
        必须保证「断线 → 锁定」的可见性先于告警处理，杜绝断线窗口期内抢发废单。
        bool 赋值在 CPython GIL 下原子，无需加锁；_connected 同理。
        """
        try:
            self._lock_down = True
            self._connected = False
            self._loop.call_soon_threadsafe(self._on_disconnect_fatal)  # type: ignore[union-attr]
        except Exception:
            # 回调线程异常绝不能冒泡到 C++（会导致 xtquant 内部崩溃）
            logger.exception("on_disconnected 处理异常，已吞并以保护 C++ 线程")

    def on_account_status(self, status: Any) -> None:
        """账号状态变动推送（C++ 线程）：解析 status_int → 投递主线程。

        Why 独立于 on_disconnected：disconnected 是连接级（socket 断），account_status
        是账号级（账号被系统停用/登录失败/穿透副链断开，socket 可能仍在）。账号被
        DISABLEBYSYS（密码错误超限）时 on_disconnected 不一定触发，必须靠本回调感知，
        否则网关以为连着继续发废单。
        """
        try:
            status_int = int(getattr(status, "status", -1))
            self._loop.call_soon_threadsafe(self._on_account_status_change, status_int)  # type: ignore[union-attr]
        except Exception:
            logger.exception("on_account_status 解析异常，已吞并以保护 C++ 线程")

    def _on_account_status_change(self, status_int: int) -> None:
        """主线程：按 8 态锁策略处理账号状态（由 on_account_status 投递）。

        - fatal 态（INVALID/FAIL/ASSIS_FAIL/DISABLEBYSYS/DISABLEBYUSER）：锁 + ERROR 告警
        - OK(0)：清锁（账号恢复正常）
        - CLOSED(6)：不锁（收盘后正常）
        - 中间态（WAITING_LOGIN/LOGINING/INITING/CORRECTING）：只 log，等后续推送
        """
        if status_int in _QMT_ACC_FATAL:
            self._lock_down = True
            logger.critical("【QMT 账号异常】status=%s account=%s 网关已锁定", status_int, self._account_id)
            _alert_account_status(self, status_int, "ERROR")
        elif status_int == _QMT_ACC_OK:
            # #6：risk_halted 期间账号 OK 只记日志，不得清锁（风控熔断需人工解除）
            if not self._risk_halted:
                self._lock_down = False
            logger.info("QMT 账号状态 OK account=%s，已清锁" if not self._risk_halted
                        else "QMT 账号状态 OK account=%s（risk_halted 粘滞，锁保持）",
                        self._account_id)
        else:
            # WAITING_LOGIN/LOGINING/INITING/CORRECTING/CLOSED 等非 fatal 态只 log
            logger.info("QMT 账号状态变动 status=%s account=%s（非 fatal，不锁）", status_int, self._account_id)

    def on_stock_order(self, order: Any) -> None:
        """委托状态变动推送（C++ 线程）：解析为内部 dict 后投递主线程。"""
        try:
            status = order.order_status
            parsed: dict[str, Any] = {
                "kind": "order",
                "order_id": order.order_id,                  # QMT 真实订单号（int）
                "stock_code": order.stock_code,
                "order_status": status,
                "state": _map_qmt_status(status),
                "order_type": getattr(order, "order_type", 0),  # #1/#5：主推路径方向来源（query_orders 同源）
                "order_volume": getattr(order, "order_volume", 0),
                "traded_volume": getattr(order, "traded_volume", 0),   # 累计成交
                "traded_price": getattr(order, "traded_price", 0.0),   # 成交均价
                "status_msg": getattr(order, "status_msg", ""),        # 废单原因等
            }
            self._loop.call_soon_threadsafe(self._process_order_update, parsed)  # type: ignore[union-attr]
        except Exception:
            logger.exception("on_stock_order 解析异常，已吞并以保护 C++ 线程")

    def on_stock_trade(self, trade: Any) -> None:
        """
        成交回报推送（C++ 线程）。

        注意 traded_volume 在 XtTrade 里是【本次成交】量（增量），与 XtOrder 的累计
        traded_volume 语义不同；上层聚合持仓时应累加 trade 事件，而非用单条覆盖。
        """
        try:
            parsed = {
                "kind": "trade",
                "order_id": trade.order_id,
                "stock_code": trade.stock_code,
                "traded_volume": getattr(trade, "traded_volume", 0),   # 本次成交量
                "traded_price": getattr(trade, "traded_price", 0.0),
                "traded_amount": getattr(trade, "traded_amount", 0.0),
                "traded_time": getattr(trade, "traded_time", 0),
                "state": OrderState.FILLED,  # 收到成交回报即视作至少部分成交
            }
            self._loop.call_soon_threadsafe(self._process_order_update, parsed)  # type: ignore[union-attr]
        except Exception:
            logger.exception("on_stock_trade 解析异常，已吞并以保护 C++ 线程")

    def on_order_error(self, order_error: Any) -> None:
        """下单失败推送（C++ 线程）：柜台拒单的具体原因（资金不足/涨跌停等）。"""
        try:
            parsed = {
                "kind": "order_error",
                "order_id": order_error.order_id,
                "error_id": getattr(order_error, "error_id", -1),
                "error_msg": getattr(order_error, "error_msg", ""),
                "state": OrderState.REJECTED,
            }
            self._loop.call_soon_threadsafe(self._process_order_update, parsed)  # type: ignore[union-attr]
        except Exception:
            logger.exception("on_order_error 解析异常，已吞并以保护 C++ 线程")

    def on_cancel_error(self, cancel_error: Any) -> None:
        """撤单失败推送（C++ 线程）：撤单被拒的原因（如订单已成交无法撤）。"""
        try:
            parsed = {
                "kind": "cancel_error",
                "order_id": cancel_error.order_id,
                "error_id": getattr(cancel_error, "error_id", -1),
                "error_msg": getattr(cancel_error, "error_msg", ""),
                "state": OrderState.FAILED,
            }
            self._loop.call_soon_threadsafe(self._process_order_update, parsed)  # type: ignore[union-attr]
        except Exception:
            logger.exception("on_cancel_error 解析异常，已吞并以保护 C++ 线程")

    def on_order_stock_async_response(self, response: Any) -> None:
        """
        异步下单回报（C++ 线程）：seq ↔ 真实 order_id 的【唯一锚点】。

        Why 此回调是整条链路最关键的缝合点：order_stock_async 只给 seq，后续推送与
        撤单都用真实 order_id；只有这里同时拿到 response.seq 与 response.order_id，
        必须在此建立 _seq_to_real 映射，否则 cancel_order 永远找不到真实单号。
        时序竞态：submit_order 返回后、本回调到达前，若上层立即撤单会因映射缺失而
        FAILED——这是已知代价，由 cancel_order 的 FAILED 文案引导上层短暂重试。
        """
        try:
            seq = response.seq
            real_order_id = response.order_id
            self._seq_to_real[seq] = real_order_id
            logger.info("QMT 异步回报锚定 seq=%s -> order_id=%s", seq, real_order_id)
            # 同步投递一条「seq 绑定」事件，便于上层把对外 seq-str 与真实单号对齐
            self._loop.call_soon_threadsafe(  # type: ignore[union-attr]
                self._process_order_update,
                {"kind": "async_response", "seq": seq, "order_id": real_order_id,
                 "state": OrderState.SUBMITTED},
            )
        except Exception:
            logger.exception("on_order_stock_async_response 解析异常，已吞并以保护 C++ 线程")

    # ------------------------------------------------------------- 内部工具
    def _resolve_real_order_id(self, order_id: str) -> Optional[int]:
        """把对外 order_id（seq-str）解析回 QMT 真实 order_id（int）。"""
        try:
            seq = int(order_id)
        except (TypeError, ValueError):
            return None
        return self._seq_to_real.get(seq)

    @staticmethod
    def _ensure_xtquant() -> None:
        """运行前置校验：xtquant 必须可用，否则后续所有 API 调用都是空指针。"""
        if not _XTQUANT_AVAILABLE:
            raise RuntimeError(
                "xtquant 未安装或不可用。QmtExecutionGateway 仅在 Windows + MiniQMT 客户端"
                "环境下可用；开发/测试环境请使用 MockExecutionGateway。"
            )
