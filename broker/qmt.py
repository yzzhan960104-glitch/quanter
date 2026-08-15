"""
broker/qmt.py
=============
QmtExecutionGateway 组装面 + 兼容 re-export 垫片（W2-H1 · broker 四文件分层）。

物理真身分三层（逻辑只搬 · spec §5.1 红线，方法体逐字保留）：
- ``broker/qmt_connection.py``（契约根 + 连接层）：模块契约/常量（xtquant 容错导入、
  order/account 11 态字面量、超时/退避/GC 常量、OrderUpdateCallback）、连接生命周期
  （connect/disconnect/L2 sid 轮换自愈）、C++ 回调骨架（XtQuantTraderCallback 8 回调）、
  连接辅助函数族（_map_qmt_status/_assert_status_contract/_cleanup_session_files 等）；
- ``broker/qmt_io.py``（IO 层 mixin）：查询六方法（_fetch_broker_positions/query_asset/
  probe_account_status/get_quote/query_orders/query_trades）——只读查询与统一降级口径；
- ``broker/qmt_business.py``（业务层 mixin）：下单/撤单三方法、撤单确认闭环、主推
  不可用惰性兜底、锁与风控状态机、订单流水 GC、回调注入与主线程回报处理、断线
  自动重连。

本模块只做两件事（同 T1 strangler 范式）：
1. **类组装**：``QmtExecutionGateway(QmtBusinessMixin, QmtIoMixin, QmtConnectionBase)``
   ——mixin 间共享状态全部走 self 属性（初始化在 QmtConnectionBase.__init__），
   MRO 上业务/IO 方法先于连接基类，外部调用面（类名/方法签名/模块级符号）零变化；
2. **显式列名 re-export**：外部 ``from broker.qmt import X``（X = 常量/辅助函数/
   xtquant 类）零改动可用——trading/qmt_gateway.py 垫片、conftest 假件注入、单测
   ``from broker import qmt as qmt_gateway`` 的属性读取均经本面。

⚠️ patch 纪律（T1 既定）：monkeypatch 内部全局（XtQuantTrader/_CONNECT_TIMEOUT/
_cleanup_session_files 等）须指「读取方真身模块」（分-layer 后多为 broker.qmt_connection）。
_ORDER_TIMEOUT 单源收口（N5 · Low ③）：qmt_io/qmt_business 均改调用点
``qmt_connection._ORDER_TIMEOUT`` 模块属性访问，patch 统一指契约根——本垫片的
re-export 副本与真身非同一对象，patch 垫片无效。

底层 API 事实来源：skills/miniqmt/references/xttrader.md（迅投官方），本模块不
臆造任何 xtquant / xtconstant 字段（CLAUDE.md 事实审查红线）。
"""

from __future__ import annotations

# === 显式列名 re-export 兼容块 =================================================
# Why 显式列名而不用 ``import *``：星号导出会静默漂移（契约根增删名不告警），显式
# 列名让「外部 import 面」成为受守卫的契约（漏一个名，依赖它的调用方立刻 ImportError
# 暴露，而非运行期 AttributeError）。分块注释标明符号归属，便于审计。
from broker.base import BaseExecutionGateway, OrderResult  # noqa: F401
from trading.compute.types import OrderRequest  # noqa: F401
from trading.types.order_state import OrderState  # noqa: F401

# --- 契约根（broker.qmt_connection）：xtquant 容错导入 + 可用性标志 ---------------
from broker.qmt_connection import (  # noqa: F401
    XtQuantTrader,
    XtQuantTraderCallback,
    StockAccount,
    xtconstant,
    _XTQUANT_AVAILABLE,
    _CallbackBase,
)
# --- 契约根（broker.qmt_connection）：常量（超时/退避/GC）+ 回调签名 -------------
from broker.qmt_connection import (  # noqa: F401
    _RECONNECT_BACKOFFS,
    _CONNECT_TIMEOUT,
    _ORDER_TIMEOUT,
    _ORDERS_GC_THRESHOLD,
    _ORDERS_GC_KEEP_SECONDS,
    OrderUpdateCallback,
    logger,
)
# --- 契约根（broker.qmt_connection）：order 11 态字面量 --------------------------
from broker.qmt_connection import (  # noqa: F401
    _QMT_ORDER_JUNK,
    _QMT_ORDER_SUCCEEDED,
    _QMT_ORDER_PART_SUCC,
    _QMT_ORDER_CANCELED,
    _QMT_ORDER_PART_CANCEL,
    _QMT_ORDER_PARTSUCC_CANCEL,
    _QMT_ORDER_REPORTED_CANCEL,
    _QMT_ORDER_REPORTED,
    _QMT_ORDER_WAIT_REPORTING,
    _QMT_ORDER_UNREPORTED,
    _QMT_ORDER_UNKNOWN,
)
# --- 契约根（broker.qmt_connection）：account 11 态字面量 + fatal 集 -------------
from broker.qmt_connection import (  # noqa: F401
    _QMT_ACC_INVALID,
    _QMT_ACC_OK,
    _QMT_ACC_WAITING_LOGIN,
    _QMT_ACC_LOGINING,
    _QMT_ACC_FAIL,
    _QMT_ACC_INITING,
    _QMT_ACC_CORRECTING,
    _QMT_ACC_CLOSED,
    _QMT_ACC_ASSIS_FAIL,
    _QMT_ACC_DISABLE_BYSYS,
    _QMT_ACC_DISABLE_BYUSER,
    _QMT_ACC_FATAL,
)
# --- 契约根（broker.qmt_connection）：模块级辅助函数族（状态映射/契约校验/告警/
#     会话清理/客户端探测/sid 轮换登记）------------------------------------------
from broker.qmt_connection import (  # noqa: F401
    _map_qmt_status,
    _assert_status_contract,
    _alert_account_status,
    _stop_trader_safely,
    _cleanup_session_files,
    _client_process_alive,
    _client_activity_age_secs,
    _client_servable,
    _drop_candidate_session_files,
    _used_session_ids,
    _candidate_session_ids,
    _write_runtime_session,
)
# --- 三层真身 mixin/基类（供组装；亦供深定制者直接引用）---------------------------
from broker.qmt_connection import QmtConnectionBase  # noqa: F401
from broker.qmt_io import QmtIoMixin  # noqa: F401
from broker.qmt_business import QmtBusinessMixin  # noqa: F401


class QmtExecutionGateway(QmtBusinessMixin, QmtIoMixin, QmtConnectionBase):
    """
    MiniQMT 实盘执行网关。

    线程模型（务必读懂再改）：
    - 主事件循环线程：connect/submit_order/cancel_order/_fetch_broker_positions 与
      _process_order_update / _on_disconnect_fatal 均在此线程跑。
    - xtquant C++ 回调线程：on_* 系列回调在此线程触发，只做「解析 + 投递」。
    - 线程池（默认 ThreadPoolExecutor）：承载 start/connect/subscribe/query/order
      等同步阻塞调用，避免它们卡死事件循环。

    断线保护：on_disconnected 触发后立即原子置位 _lock_down，submit_order 据此
    熔断拒单，杜绝断线窗口期内的废单重发（CLAUDE.md 状态机边界红线）。

    分层组装（W2-H1）：本类为零方法体的组装点——业务/IO 方法先入 MRO（mixin），
    连接生命周期/C++ 回调/共享状态初始化在 QmtConnectionBase（基类）。MRO：
    QmtExecutionGateway → QmtBusinessMixin → QmtIoMixin → QmtConnectionBase →
    BaseExecutionGateway → _CallbackBase（XtQuantTraderCallback 或 object）。
    """
