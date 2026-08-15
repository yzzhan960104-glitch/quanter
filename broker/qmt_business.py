"""
broker/qmt_business.py
======================
QmtExecutionGateway 业务层 mixin（W2-H1 · broker 四文件分层 · 逻辑只搬）。

接缝说明（为什么在这层）：
- 本模块承载网关的【订单业务与状态机】：下单/撤单三方法（submit_order /
  cancel_order / cancel_order_by_broker_oid）、撤单确认闭环与主推不可用惰性兜底
  （_confirm_cancelled / _sync_orders_if_stale）、锁与风控状态机（is_blocked /
  set_risk_halt / clear_risk_halt / is_locked）、订单流水查询与 GC（get_order /
  cleanup_orders）、上层回调注入与主线程回报处理（set_order_update_callback /
  _process_order_update）、断线告警与指数退避自动重连（_on_disconnect_fatal /
  _reconnect）——共性是「写订单状态、动锁、产生副作用」，与 IO 层的只读查询相对。
- 类组装于 broker/qmt.py：``QmtExecutionGateway(QmtBusinessMixin, QmtIoMixin,
  QmtConnectionBase)``。共享状态全部走 self 属性（_orders/_seq_to_real/
  _main_push_available/_lock_down/_risk_halted/_reconnecting 等，初始化在
  QmtConnectionBase.__init__）——与分层前同一实例世界，mixin 间零新通信机制。
- 共享常量从 broker.qmt_connection（契约根）from-import：不可变值拷贝语义。
  xtconstant 例外说明：submit_order 读 xtconstant.STOCK_BUY/SELL 拿的是本模块
  import 时绑定的契约根副本（conftest 假件先于本模块装载注入，绑定即假件）。
  **_ORDER_TIMEOUT 例外（N5 · Low ③ 单源收口）**：超时常量改
  ``qmt_connection._ORDER_TIMEOUT`` 调用点模块属性访问——单测 patch 超时统一指
  契约根 ``patch("broker.qmt_connection._ORDER_TIMEOUT")``（一处生效全部读取方，
  io/business/契约根自身），不再各自维护 from-import 副本（原三拷贝运行期调
  超时要踩三处）。

分层红线（spec §5.1）：逻辑只搬位置 + 接缝注释，零行为改动；方法体逐字保留。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping, Optional

from broker.base import OrderResult
# N5（Low ③）：_ORDER_TIMEOUT 从 from-import 列表移除，改 ``qmt_connection.
# _ORDER_TIMEOUT`` 调用点模块属性访问——契约根单源，patch 一处生效三读取方。
from broker import qmt_connection
from broker.qmt_connection import (
    _ORDERS_GC_KEEP_SECONDS,
    _ORDERS_GC_THRESHOLD,
    _RECONNECT_BACKOFFS,
    OrderUpdateCallback,
    xtconstant,
)
from trading.compute.types import OrderRequest
from trading.types.order_state import OrderState  # Layer2 follow-up #4c：改指 types 真身

# ⚠️ 日志名锁定 "broker.qmt"（不用默认 __name__）：分层前全部日志经 broker.qmt 一个
# logger 出；分层后三文件共用同名 logger，保 caplog 过滤（test_qmt_alert_degradation
# _logged 按 logger="broker.qmt" 捕获断线/重连告警软降级 debug）与运维日志口径零变化。
logger = logging.getLogger("broker.qmt")


class QmtBusinessMixin:
    """QmtExecutionGateway 的业务层（下单/撤单/锁风控状态机/回报处理/自动重连）。"""

    # ----------------------------------------------------- M2 撤单确认闭环
    async def _confirm_cancelled(self, oid: str, timeout: float = 5.0, interval: float = 0.5) -> bool:
        """撤单后轮询确认到终态（CANCELLED/FILLED/REJECTED/PARTIAL_CANCELLED），超时返 False。

        物理意图（M2 · [[qmt-live-smoke-findings]] 撤单主推延迟1-2s）：
            cancel_order 调用后 QMT 主推回报有 1-2s 延迟，若不主动确认，撤单状态悬空
            （本地以为撤了、柜台其实没撤）。本方法轮询 query_orders 直到该 oid 到终态
            或超时，让上层据 True/False 决定是否告警/重试。

        返回：
            True  = 已确认到终态（CANCELLED 撤成 / FILLED 撤前已成交 / REJECTED 拒单 /
                    PARTIAL_CANCELLED 部分撤）
            False = 超时未确认（调用方须记 WARNING，绝不假装撤成功）

        边界：
            lock_down/未连接 → query_orders 已降级返[] → 本方法自然超时返 False（不抛）。
            撤单低频（pre_open每日1次+少量pending），0.5s 间隔撞柜台限频风险可接受。
        """
        deadline = time.monotonic() + timeout
        # 终态集合：撤单成功的 CANCELLED + 撤单时已成交的 FILLED + 拒单 REJECTED + 部分撤
        terminal = (OrderState.CANCELLED, OrderState.FILLED, OrderState.REJECTED,
                    OrderState.PARTIAL_CANCELLED)
        while time.monotonic() < deadline:
            try:
                orders = await self.query_orders(cancelable_only=False)
            except Exception:
                # query_orders 内部异常已吞返[]，双保险
                orders = []
            for o in orders:
                if str(o.get("order_id")) == str(oid):
                    if o.get("state") in terminal:
                        return True
                    break  # 找到但非终态，本轮等下一轮轮询
            await asyncio.sleep(interval)
        return False

    # ----------------------------------------------------- 主推不可用惰性兜底
    async def _sync_orders_if_stale(self) -> int:
        """主推不可用时惰性同步订单状态（subscribe 失败兜底，T5）。

        策略：
        - _main_push_available=True（subscribe 成功，主推正常）→ no-op 返 0；
        - _main_push_available=False（subscribe 失败，主推缺失）→ 调 query_orders
          主动拉当日委托，逐条 merge 进 self._orders，返同步的笔数。

        Why 惰性而非后台定时轮询：
        - 引入后台轮询会带来新调度复杂度（生命周期管理 / 关停时序 / 与断线重连的
          竞态），且颈线法触发点本就低频（pre_open/stop_loss_monitor），触发点前
          补全足以覆盖盲区风险，查询开销可接受；
        - 主推可用时直接 no-op，零开销——避免误触主推正常场景的 query_stock_orders
          （可能撞柜台限频，与 query_asset 同型降级风险）。

        调用时机（由上层 engine 决定，非本网关职责）：
        pre_open / stop_loss_monitor 等依赖 _orders 状态的触发点前，上层先
        await 本方法兜底。engine 接入是消费者逻辑，留 follow-up（本 task 只提供
        方法 + 标志）。

        state 类型契约（T4 fix 已对齐）：query_orders 返的 state 已是 OrderState
        枚举（非 .name 字符串），与 _process_order_update 写的 _orders 枚举世界 +
        circuit_breaker._TERMINAL（frozenset[OrderState]）同型。本方法直接透传
        merge，**不做类型转换**——若未来 query_orders 改返字符串，circuit_breaker
        会踩「OrderState.FILLED not in {...字符串...} 恒 True → 已成交单误判非终态」
        陷阱，那时应在 query_orders 侧修，非本方法。

        返回：同步进 self._orders 的笔数（主推可用/查询异常时返 0）。
        """
        if self._main_push_available:
            # 主推正常：_orders 已被 on_stock_order 回调实时推进，无需查询
            return 0
        try:
            # 复用 T4 的 query_orders（cancelable_only=False 全量委托，含已终态）
            orders = await self.query_orders()
        except Exception:
            # query_orders 内部异常已吞并返 []，这里是双保险（如 monkeypatch 注入
            # 异常时）；本轮跳过，不阻塞触发点主流程
            logger.exception("_sync_orders_if_stale 查询失败，本轮跳过")
            return 0
        n = 0
        for o in orders:
            # order_id 统一转 str 做 key（与 _process_order_update 同口径）
            oid = str(o.get("order_id", ""))
            if not oid:
                continue
            # rec 结构与 _process_order_update 写的兼容：state 透传（枚举直接用，
            # 不转换），_gc_ts 补 GC 时间戳（#10 终态单超期清理基准）
            rec = dict(o)
            rec["_gc_ts"] = time.time()
            self._orders[oid] = rec
            n += 1
        if n:
            logger.info("惰性同步补全 %s 笔委托（主推不可用兜底）", n)
        return n

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
        if self.is_blocked:
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
            # #9：wait_for 兜底防 order_stock_async 永久阻塞事件循环（超时返 FAILED）。
            seq = await asyncio.wait_for(
                self._loop.run_in_executor(None, _do_order), timeout=qmt_connection._ORDER_TIMEOUT)
        except Exception as exc:
            # C++ 调用异常/超时（如会话失效）：记 FAILED 而非冒泡，让上层状态机兜底
            logger.exception("QMT order_stock_async 异常/超时 symbol=%s", order.symbol)
            return OrderResult(order_id=order.order_id or "", state=OrderState.FAILED,
                               message=f"下单异常/超时(>{qmt_connection._ORDER_TIMEOUT}s)：{exc}")

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
        if self.is_blocked or not self._connected:
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
            # #9：wait_for 兜底防 cancel_order_stock 永久阻塞事件循环（超时返 FAILED）。
            rc = await asyncio.wait_for(
                self._loop.run_in_executor(None, _do_cancel), timeout=qmt_connection._ORDER_TIMEOUT)
        except Exception as exc:
            logger.exception("QMT cancel_order_stock 异常/超时 order_id=%s", order_id)
            return OrderResult(order_id=order_id, state=OrderState.FAILED,
                               message=f"撤单异常/超时(>{qmt_connection._ORDER_TIMEOUT}s)：{exc}")

        if rc == 0:
            # rc==0 仅表「撤单指令已成功发出」，非订单终态——柜台可能因订单已成交 /
            # 已撤而后续撤单失败（由 on_cancel_error / on_stock_order 推送推进）。
            # 最终态以 on_stock_order 主推的 CANCELLED 为准；message 显式标注非终态，
            # 防上层（engine/策略）误读为「撤单已成功」而错算敞口（风控拷问：敞口
            # 错算=重复发单或漏对冲，实盘致命）。
            return OrderResult(order_id=order_id, state=OrderState.CANCELLED,
                               message="撤单指令已发出（非终态），最终态以 on_stock_order 推送 CANCELLED 为准")
        return OrderResult(order_id=order_id, state=OrderState.FAILED,
                           message=f"撤单失败 rc={rc}")

    async def cancel_order_by_broker_oid(self, broker_oid: int) -> OrderResult:
        """按柜台真实 order_id 撤单（state-store-redesign §3.5）。

        物理意图（P0-4 根因修复）：cancel_all_open_orders 旧路径遍历 gw._orders 内存，
        取出的 oid 在 live 下是柜台真实单号（_process_order_update 按 order.order_id 键），
        再调 cancel_order(oid) 走 _resolve_real_order_id(seq→real 查表) —— 真实单号不是 seq，
        查表必返 None → 撤单 FAILED「映射缺失」。本方法绕过 seq→real 映射，直接用柜台单号调
        cancel_order_stock，让 cancel_all_open_orders（从 query_orders 拿到的就是柜台单号）能撤成。

        与 cancel_order 的区别：cancel_order 入参是 seq-str（对外暴露的 order_id，走映射）；
        本方法入参是 int 柜台单号（query_orders 返回的 order_id 字段，直接透传给底层 SDK）。

        Args:
            broker_oid: QMT 柜台真实 order_id（int），来自 query_orders(cancelable_only=True)。
        """
        if self.is_blocked or not self._connected:
            return OrderResult(order_id=str(broker_oid), state=OrderState.REJECTED,
                               message="网关未连接或已锁定，撤单失败")

        def _do_cancel() -> int:
            # 直接用柜台单号撤，不走 _seq_to_real 映射（绕过 P0-4 根因）
            return self._trader.cancel_order_stock(self._account, int(broker_oid))

        try:
            rc = await asyncio.wait_for(
                self._loop.run_in_executor(None, _do_cancel), timeout=qmt_connection._ORDER_TIMEOUT)
        except Exception as exc:
            logger.exception("QMT cancel_order_by_broker_oid 异常/超时 broker_oid=%s", broker_oid)
            return OrderResult(order_id=str(broker_oid), state=OrderState.FAILED,
                               message=f"撤单异常/超时(>{qmt_connection._ORDER_TIMEOUT}s)：{exc}")

        if rc == 0:
            return OrderResult(order_id=str(broker_oid), state=OrderState.CANCELLED,
                               message="撤单指令已发出（非终态），最终态以 on_stock_order 推送 CANCELLED 为准")
        return OrderResult(order_id=str(broker_oid), state=OrderState.FAILED,
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
    def is_blocked(self) -> bool:
        """网关拒单总闸（#6）：风控熔断或断线锁任一生效即拒。"""
        return self._risk_halted or self._lock_down

    def set_risk_halt(self, halted: bool = True) -> None:
        """风控熔断锁（emergency_halt/日内-3% 触发）。halted=True 置粘滞锁 + _lock_down。"""
        self._risk_halted = halted
        if halted:
            self._lock_down = True
            self._connected = False

    def clear_risk_halt(self) -> None:
        """显式解除风控熔断（人工/次日盘前）。仅清 risk_halt，_lock_down 由 connect 自然恢复。"""
        self._risk_halted = False

    @property
    def is_locked(self) -> bool:
        """断线锁定标志（风控层据此熔断发单与对账）。"""
        return self._lock_down

    def get_order(self, order_id: str) -> Optional[Mapping[str, Any]]:
        """查询本地缓存的最新订单回报（主线程同步读，无锁安全）。"""
        return self._orders.get(order_id)

    def cleanup_orders(self, keep_seconds: float = _ORDERS_GC_KEEP_SECONDS) -> int:
        """GC 终态且超 keep_seconds 的订单流水（#10 防内存泄漏）。

        保留：非终态单（SUBMITTED/PARTIAL_FILLED 等待回报推进）+ 终态但未超期（近 N 日对账窗口）。
        删除：终态（FILLED/CANCELLED/REJECTED/FAILED/PARTIAL_CANCELLED）且 _gc_ts 超期。
        注：_seq_to_real/_seq_to_client（seq 解耦映射）量小且清理有撤单时序风险（async_response
        与 on_stock_order 时序窗口内映射仍需保留），暂不 GC，留 follow-up。
        """
        now = time.time()
        terminal = {
            OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED,
            OrderState.FAILED, OrderState.PARTIAL_CANCELLED,
        }
        stale = [
            oid for oid, rec in self._orders.items()
            if rec.get("state") in terminal
            and now - rec.get("_gc_ts", now) > keep_seconds
        ]
        for oid in stale:
            del self._orders[oid]
        if stale:
            logger.info("QMT 订单流水 GC：删除 %s 条终态超期单（保留 %s 条）",
                        len(stale), len(self._orders))
        return len(stale)

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
            # #1：merge 而非覆盖——on_stock_trade/async_response 的 dict 不含 order_type，
            # 覆盖会让 _order_direction 内存兜底失效（主推路径方向恒 None 的历史根因之一）。
            rec = dict(self._orders.get(order_id, {}))
            rec.update(update)
            rec["_gc_ts"] = time.time()   # #10 GC 时间基准（终态单按此判定超期）
            self._orders[order_id] = rec  # type: ignore[assignment]
        if len(self._orders) > _ORDERS_GC_THRESHOLD:
            self.cleanup_orders()

        if self._on_order_update is not None:
            try:
                # 异步副作用交给事件循环调度；本同步函数立即返回不阻塞
                self._loop.create_task(self._on_order_update(update))  # type: ignore[union-attr]
            except RuntimeError:
                # 事件循环已关闭（如进程退出期）：丢弃回调，避免「无 loop 可调度」异常
                logger.warning("事件循环不可用，丢弃一次订单回报回调 order_id=%s", order_id)

    def _on_disconnect_fatal(self) -> None:
        """
        主线程：断线告警 + 启动自动重连（由 on_disconnected 经 call_soon_threadsafe 投递）。

        Why 单列主线程处理：on_disconnected 在 C++ 线程，不能直接发钉钉报警协程；
        投递到主线程后，此处方可安全 create_task 触发告警 + 重连。锁定标志已在
        C++ 线程率先置位（见 on_disconnected），此处只负责告警与重连调度。

        B-8 修复：旧实现仅 critical 日志「请人工重新 connect()」无告警通道，断线期间持仓
        离场停摆、敞口失控。现 _on_disconnect_fatal 启动指数退避自动重连（_reconnect）
        + 钉钉告警（fire_and_forget WARN 推送），重连耗尽由 _reconnect 发 ERROR 告警。
        """
        logger.critical(
            "【QMT 断线】account=%s 网关已锁定，启动自动重连（指数退避 %s）...",
            self._account_id, _RECONNECT_BACKOFFS,
        )
        # 钉钉告警（fire_and_forget 跨线程安全，链路异常被吞不影响重连主路径）
        try:
            from infra.notifier import NotificationManager, fire_and_forget
            fire_and_forget(NotificationManager.get_default().notify_risk_event(
                f"QMT 断线，启动自动重连 account={self._account_id}", "WARN"))
        except Exception:
            # G7 告警可观测：断线告警通道软降级（控制流不变），加 debug 消盲区。
            logger.debug("告警通道软降级：断线告警发送失败 account=%s",
                         getattr(self, "_account_id", "?"), exc_info=True)
        # 在持久 loop 上调度重连任务（loop 关闭/为空时不调度，防 shutdown 期徒劳重连）
        if self._loop is not None and not self._loop.is_closed():
            self._loop.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """断线后指数退避自动重连（B-8）：最多 len(_RECONNECT_BACKOFFS) 次，每次失败告警。

        - 重连成功 → connect() 内部清 lock_down/置 connected，下轮 beat 自动恢复 live；
        - 全部失败 → 保持锁态（connect 失败已置 lock_down=True）+ ERROR 告警等人工；
        - 退避 sleep 期间 lock_down=True，submit_order 被网关拒，tick_exit 优雅 no-op。

        M1 互斥：on_disconnected→_reconnect 与 T8 健康守护 job 是两条重连路径，共用本
        入口。用 _reconnecting 软锁保证同一时刻只有一条路径在跑重连，避免并发 connect
        同一 sid 触发 QMT 返回 -1（session 占用）的自扰死循环。
        """
        # M1 互斥入口：已在重连则直接让出（另一条路径正在 connect，本路径不应叠加）
        if self._reconnecting:
            return
        self._reconnecting = True
        try:
            from infra.notifier import NotificationManager, fire_and_forget
            # 防御：重连期间确保锁态（拒新单）；connect 成功会清锁，失败/耗尽保持锁。
            self._lock_down = True
            n = len(_RECONNECT_BACKOFFS)
            for i, delay in enumerate(_RECONNECT_BACKOFFS, 1):
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    await self.connect()
                    logger.info("QMT 重连成功（第 %s/%s 次）", i, n)
                    try:
                        fire_and_forget(NotificationManager.get_default().notify_risk_event(
                            f"QMT 断线后重连成功（第{i}次）", "INFO"))
                    except Exception:
                        # G7 告警可观测：重连成功告警通道软降级（控制流不变），加 debug 消盲区。
                        logger.debug("告警通道软降级：重连成功告警发送失败 第%s次", i, exc_info=True)
                    return
                except Exception as exc:
                    logger.warning("QMT 重连失败（第 %s/%s）：%s", i, n, exc)
                    try:
                        fire_and_forget(NotificationManager.get_default().notify_risk_event(
                            f"QMT 重连失败第{i}次：{exc}", "WARN"))
                    except Exception:
                        # G7 告警可观测：重连失败告警通道软降级（控制流不变），加 debug 消盲区。
                        logger.debug("告警通道软降级：重连失败告警发送失败 第%s次", i, exc_info=True)
            logger.critical("QMT 重连耗尽（%s 次），网关保持锁态，请人工介入", n)
            try:
                fire_and_forget(NotificationManager.get_default().notify_risk_event(
                    f"QMT 重连耗尽（{n}次），网关锁态，请人工介入！", "ERROR"))
            except Exception:
                # G7 告警可观测：重连耗尽告警通道软降级（控制流不变），加 debug 消盲区。
                # 重连耗尽是致命态，告警通道此处失效会让运维完全失明——debug 仍留痕。
                logger.debug("告警通道软降级：重连耗尽告警发送失败（%s次）", n, exc_info=True)
        finally:
            # M1：无论重连成功/失败/异常，释放互斥锁，允许下一次断线再触发重连。
            # 放 finally 保证异常路径（如 KeyboardInterrupt）不遗留死锁标志。
            self._reconnecting = False
