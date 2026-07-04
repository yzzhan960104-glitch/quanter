"""
trading/qmt_gateway.py
======================
QmtExecutionGateway —— 迅投 MiniQMT (xtquant) 异步执行网关。

设计骨架（架构师视角的三条不可逾越红线）：
1. **同步 C++ ↔ 异步 FastAPI 的线程边界**：xtquant 是同步阻塞的 C++ 绑定，所有
   会阻塞事件循环的调用（start/connect/subscribe/query/order/cancel/stop）必须经
   ``loop.run_in_executor`` 投递到线程池，绝不在协程里直调。
2. **C++ 回调线程 ↔ 主事件循环的状态边界**：XtQuantTraderCallback 的回调运行在
   xtquant 内部 C++ 线程，回调里【只做解析 + call_soon_threadsafe 投递】，绝不
   直接改 FastAPI State、绝不直接 await 钉钉报警——否则轻则竞态，重则跨线程
   持有未完成的协程导致事件循环僵死。
3. **seq ↔ 真实 order_id 的契约边界**：order_stock_async 仅返回请求序号 seq，
   而后续 on_stock_order / on_stock_trade 推送与 cancel_order_stock 用的都是
   柜台真实 order_id；必须以 on_order_stock_async_response 为唯一锚点建映射表，
   否则撤单与回报匹配整体断裂。

底层 API 事实来源：skills/miniqmt/references/xttrader.md（迅投官方），本模块不
臆造任何 xtquant / xtconstant 字段（CLAUDE.md 事实审查红线）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Mapping, Optional

from trading.execution_gateway import BaseExecutionGateway, OrderRequest, OrderResult
from trading.order_state import OrderState

logger = logging.getLogger(__name__)

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
    if status in (_QMT_ORDER_CANCELED, _QMT_ORDER_REPORTED_CANCEL):
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
    """
    if not _XTQUANT_AVAILABLE:
        return  # 无 xtquant 时无对象可校验，由 _ensure_xtquant 在连接处拦
    expected = {
        "ORDER_JUNK": _QMT_ORDER_JUNK,
        "ORDER_SUCCEEDED": _QMT_ORDER_SUCCEEDED,
        "ORDER_PART_SUCC": _QMT_ORDER_PART_SUCC,
        "ORDER_CANCELED": _QMT_ORDER_CANCELED,
        "ORDER_PART_CANCEL": _QMT_ORDER_PART_CANCEL,
        "ORDER_REPORTED": _QMT_ORDER_REPORTED,
        "ORDER_UNREPORTED": _QMT_ORDER_UNREPORTED,
    }
    drifted = [f"{n}={getattr(xtconstant, n)}≠{v}" for n, v in expected.items()
               if getattr(xtconstant, n, None) is not None and getattr(xtconstant, n) != v]
    if drifted:
        raise RuntimeError(f"xtconstant 枚举契约漂移：{drifted}，请核对 xttrader.md 后更新本模块")


class QmtExecutionGateway(BaseExecutionGateway, _CallbackBase):  # type: ignore[misc]
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
        # 断线锁定：初始即 True，connect 成功才放开；断线立刻回锁，风控层据此熔断
        self._lock_down: bool = True

        # seq ↔ 真实 order_id ↔ 客户端单号 的三向映射（撤单与回报匹配的唯一依据）
        self._seq_to_real: dict[int, int] = {}     # seq -> QMT 柜台真实 order_id
        self._seq_to_client: dict[int, str] = {}   # seq -> 调用方透传的客户端单号

        # 订单回报流水：主线程独占读写，供上层 query 与对账（call_soon_threadsafe 保证）
        self._orders: dict[str, dict[str, Any]] = {}

        # 上层注入的异步回报回调（钉钉报警 / State 持久化），主线程 create_task 调度
        self._on_order_update: Optional[OrderUpdateCallback] = None

    # ------------------------------------------------------------------ 连接
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
        """
        self._loop = asyncio.get_running_loop()
        self._ensure_xtquant()
        _assert_status_contract()

        # 1. 建实例 + 注册自身为回调（register_callback 必须在 start 之前）
        self._trader = XtQuantTrader(self._userdata_path, self._session_id)
        self._trader.register_callback(self)
        # StockAccount 构造是纯 Python 内存操作，无需线程池
        self._account = StockAccount(self._account_id)

        # 2. start/connect/subscribe 同步阻塞，统一投线程池
        def _bootstrap() -> tuple[int, int]:
            self._trader.start()
            connect_rc = self._trader.connect()
            if connect_rc != 0:
                # 连接失败时不必 subscribe，直接返回，由外层判定
                return connect_rc, -1
            sub_rc = self._trader.subscribe(self._account)
            return connect_rc, sub_rc

        connect_rc, sub_rc = await self._loop.run_in_executor(None, _bootstrap)

        if connect_rc != 0:
            # connect 返回非 0 即连接失败（xttrader.md：返回 0 表示成功）
            self._lock_down = True
            raise ConnectionError(
                f"QMT connect 失败，返回码={connect_rc}（0=成功）；"
                f"请确认 MiniQMT 客户端已启动且 userdata_mini 路径正确：{self._userdata_path}"
            )
        if sub_rc != 0:
            # subscribe 失败不致命但危险：拿不到主推回报，订单状态只能靠主动查询
            logger.warning(
                "QMT subscribe 返回 %s（0=成功，-1=失败），委托/成交主推可能缺失，"
                "订单状态将退化为主动查询模式", sub_rc
            )

        self._connected = True
        self._lock_down = False  # 连接成功，解除发单锁定
        logger.info("QMT 网关已连接 account=%s session=%s", self._account_id, self._session_id)

    async def disconnect(self) -> None:
        """优雅断开：stop() 同步阻塞，投线程池；无条件回锁防断开瞬间的发单竞态。"""
        if self._trader is not None and self._loop is not None:
            await self._loop.run_in_executor(None, self._trader.stop)
        self._connected = False
        self._lock_down = True
        logger.info("QMT 网关已断开 account=%s", self._account_id)

    # ---------------------------------------------------------- 持仓对账
    async def _fetch_broker_positions(self) -> Mapping[str, float]:
        """
        拉取券商真实持仓并清洗为 {stock_code: volume}（模板方法 _fetch_broker_positions 实现）。

        边界与清洗（Grill Me）：
        - query_stock_positions 返回 None：文档明确「查询失败或当日持仓为空」均返回
          None，二者不可区分，这里统一记 warning 并返回空 dict，避免对账层把「查询
          失败」误当「真实空仓」而触发 only_broker 漂移告警。
        - can_use_volume == 0 过滤：T+1 当日买入仓位可用为 0 但确属真实持仓；此处按
          调用方契约过滤「废弃持仓」，意味着本网关对账口径是【可操作持仓】而非【全量
          持仓】。若策略层需要全量敞口对账，应另起查询口径，不可复用本返回值。
        - volume 转 float：QMT 返回 int（股数），对外契约统一 float 以兼容碎股/债券张数。
        """
        if self._loop is None or self._trader is None or self._account is None:
            raise RuntimeError("QMT 网关未连接，无法对账（请先 await connect()）")
        if self._lock_down:
            raise RuntimeError("QMT 网关已锁定（断线保护），拒绝对账以防脏读")

        positions = await self._loop.run_in_executor(
            None, lambda: self._trader.query_stock_positions(self._account)
        )
        if positions is None:
            logger.warning("query_stock_positions 返回 None（查询失败或当日无持仓）")
            return {}

        cleaned: dict[str, float] = {}
        for p in positions:
            # 过滤可用为 0 的废弃持仓（已平仓残留 / T+1 冻结不可操作仓）
            if getattr(p, "can_use_volume", 0) == 0:
                continue
            cleaned[p.stock_code] = float(p.volume)
        logger.debug("QMT 对账拉取完成：有效持仓 %d 只", len(cleaned))
        return cleaned

    # -------------------------------------------------------------- 下单
    async def submit_order(self, order: OrderRequest) -> OrderResult:
        """
        异步下单（BaseExecutionGateway.submit_order 实现）。

        映射契约（来源 xttrader.md「股票异步报单」+「报价类型」）：
        - side="buy"  -> xtconstant.STOCK_BUY；否则 STOCK_SELL。
        - price 为 None -> 市价单 price_type=LATEST_PRICE，price 传 0.0 占位
          （文档未显式约定市价单 price 取值，惯例传 0；LATEST_PRICE 仅实盘生效，
          模拟环境不支持市价报单——属已知边界，实盘前须在仿真环境验证）。
        - price 有值 -> 限价单 price_type=FIX_PRICE，price= float(order.price)。

        返回契约：
        - seq > 0：转 str 作 order_id 返回，状态 SUBMITTED（用户规格要求）。
        - seq == -1：柜台拒单，返回 REJECTED。
        Why order_stock_async 仍投线程池：它虽以 async 命名，实为「同步返回 seq +
        回调推送结果」的语义，底层仍是 C++ 同步调用，可能因柜台通信而短暂阻塞。
        """
        if self._loop is None or self._trader is None or self._account is None:
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED,
                               message="网关未连接，拒单")
        if self._lock_down:
            # 断线熔断：宁可拒单也不发废单（断线窗口期重发=重复持仓风险）
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED,
                               message="网关已锁定（断线保护），禁止发单")
        if not self._connected:
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED,
                               message="未连接，拒单")

        # 买卖方向映射
        order_type = xtconstant.STOCK_BUY if order.side == "buy" else xtconstant.STOCK_SELL
        # 报价类型：None=市价(LATEST_PRICE)，有值=限价(FIX_PRICE)
        if order.price is None:
            price_type = xtconstant.LATEST_PRICE
            price = 0.0  # 市价单价格占位
        else:
            price_type = xtconstant.FIX_PRICE
            price = float(order.price)

        # order_volume 文档要求 int（股数）；A 股 100 整数倍约束由上层引擎/状态机保证
        volume = int(order.qty)
        # order_remark 文档约束最大 24 个英文字符，透传客户端单号便于回报对账（超长截断）
        remark = (order.order_id or self._strategy_name)[:24]

        def _do_order() -> int:
            return self._trader.order_stock_async(
                self._account,        # StockAccount
                order.symbol,         # 证券代码，如 '600000.SH'
                order_type,           # STOCK_BUY / STOCK_SELL
                volume,               # 委托数量（int，股）
                price_type,           # LATEST_PRICE / FIX_PRICE
                price,                # 限价单为委托价，市价单为 0.0
                self._strategy_name,  # 策略名（QMT 端归类）
                remark,               # 委托备注（<=24 英文字符）
            )

        try:
            seq = await self._loop.run_in_executor(None, _do_order)
        except Exception as exc:
            # C++ 调用异常（如会话失效）：记 FAILED 而非冒泡，让上层状态机兜底
            logger.exception("QMT order_stock_async 异常 symbol=%s", order.symbol)
            return OrderResult(order_id=order.order_id or "", state=OrderState.FAILED,
                               message=f"下单异常：{exc}")

        if seq is None or seq < 0:
            # seq == -1：柜台拒单（资金不足/涨跌停/参数非法等），具体原因由 on_order_error 推送
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED,
                               message=f"QMT 拒单 seq={seq}")

        # 登记客户端单号映射，待 on_order_stock_async_response 回调补全真实 order_id
        self._seq_to_client[seq] = order.order_id or str(seq)
        # 对外 order_id 用 seq 的字符串形式（用户规格要求）
        return OrderResult(
            order_id=str(seq),
            state=OrderState.SUBMITTED,
            message="已提交，等待柜台回报（真实 order_id 待 async_response 回调补全）",
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        """
        撤单（BaseExecutionGateway.cancel_order 实现）。

        致命细节：cancel_order_stock 需要的是 QMT 柜台真实 order_id（int），而对外
        暴露的 order_id 是 submit_order 返回的 seq-str。必须经 _seq_to_real 查表换
        出真实 order_id；若 async_response 回调未到（映射缺失），撤单无法发出——
        这是 seq/real 解耦的固有代价，返回 FAILED 让上层短延迟后重试。
        """
        if self._lock_down or not self._connected:
            return OrderResult(order_id=order_id, state=OrderState.REJECTED,
                               message="网关未连接或已锁定，撤单失败")
        real_order_id = self._resolve_real_order_id(order_id)
        if real_order_id is None:
            return OrderResult(
                order_id=order_id, state=OrderState.FAILED,
                message="真实 order_id 尚未回报（seq→order_id 映射缺失），请短暂延迟后重试",
            )

        def _do_cancel() -> int:
            # cancel_order_stock：0=成功发出撤单指令，-1=失败（xttrader.md「股票同步撤单」）
            return self._trader.cancel_order_stock(self._account, real_order_id)

        try:
            rc = await self._loop.run_in_executor(None, _do_cancel)
        except Exception as exc:
            logger.exception("QMT cancel_order_stock 异常 order_id=%s", order_id)
            return OrderResult(order_id=order_id, state=OrderState.FAILED,
                               message=f"撤单异常：{exc}")

        if rc == 0:
            # 撤单指令已发出，最终状态以 on_stock_order 推送的 CANCELLED 为准
            return OrderResult(order_id=order_id, state=OrderState.CANCELLED,
                               message="撤单指令已发出，等待回报确认")
        return OrderResult(order_id=order_id, state=OrderState.FAILED,
                           message=f"撤单失败 rc={rc}")

    # ---------------------------------------------------- 回调注入与查询
    def set_order_update_callback(self, cb: OrderUpdateCallback) -> None:
        """
        注入上层异步回报回调（钉钉报警 / State 持久化 / DB 写入）。

        Why 必须是 async：回调由主线程 _process_order_update 经 create_task 调度，
        绝不在 C++ 回调线程里直接执行——这是「回调不改 State、不直接报警」红线的
        落地方式：C++ 线程只投递，主线程只调度，副作用在主线程的协程里安全发生。
        """
        self._on_order_update = cb

    @property
    def is_locked(self) -> bool:
        """断线锁定标志（风控层据此熔断发单与对账）。"""
        return self._lock_down

    def get_order(self, order_id: str) -> Optional[Mapping[str, Any]]:
        """查询本地缓存的最新订单回报（主线程同步读，无锁安全）。"""
        return self._orders.get(order_id)

    # ------------------------------------------------- 主线程处理（被投递）
    def _process_order_update(self, update: Mapping[str, Any]) -> None:
        """
        主线程同步：更新本地订单流水 + 触发上层异步回报回调。

        Why 这里是线程边界的「安全岸」：本函数由 call_soon_threadsafe 投递，必定在
        主事件循环线程执行，因此对 self._orders 的读写无锁安全；上层异步副作用通过
        create_task 调度，避免本函数（同步）去 await 协程而阻塞事件循环。
        """
        # order_id 统一转 str 做 key（兼容 on_stock_order 的 int 真实单号与 seq-str）
        order_id = str(update.get("order_id", ""))
        if order_id:
            self._orders[order_id] = dict(update)  # type: ignore[assignment]

        if self._on_order_update is not None:
            try:
                # 异步副作用交给事件循环调度；本同步函数立即返回不阻塞
                self._loop.create_task(self._on_order_update(update))  # type: ignore[union-attr]
            except RuntimeError:
                # 事件循环已关闭（如进程退出期）：丢弃回调，避免「无 loop 可调度」异常
                logger.warning("事件循环不可用，丢弃一次订单回报回调 order_id=%s", order_id)

    def _on_disconnect_fatal(self) -> None:
        """
        主线程：断线告警处理（由 on_disconnected 经 call_soon_threadsafe 投递）。

        Why 单列主线程处理：on_disconnected 在 C++ 线程，不能直接发钉钉报警协程；
        投递到主线程后，此处方可安全 create_task 触发最高级别告警。锁定标志已在
        C++ 线程率先置位（见 on_disconnected），此处只负责告警与状态清理。
        """
        logger.critical(
            "【QMT 断线】account=%s 网关已锁定，禁止后续发单！请人工介入检查 MiniQMT 客户端"
            "与网络后，重新 await connect() 复位", self._account_id
        )
        # TODO(风控): 此处可 create_task 触发钉钉/企业微信最高级别报警；
        #   报警协程应由上层注入，网关不直接依赖 notifier，保持解耦。

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
