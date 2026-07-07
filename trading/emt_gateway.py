"""
trading/emt_gateway.py
======================
EmtExecutionGateway —— 东方财富证券 EMT 极速交易（vnemttrader）异步执行网关。

设计骨架（与 QmtExecutionGateway 同构，三条红线一致）：
1. **同步 C++ ↔ 异步 FastAPI 的线程边界**：login/insertOrder/cancelOrder/queryAsset/
   queryPosition 等同步阻塞调用经 ``loop.run_in_executor`` 投线程池，绝不卡事件循环。
2. **C++ 回调线程 ↔ 主事件循环的状态边界**：onOrderEvent/onTradeEvent 等回调运行在
   EMT 内部 C++ 线程，只做解析 + ``call_soon_threadsafe`` 投递，绝不直接改 State。
3. **order_emt_id 契约**：insertOrder 返回 order_emt_id（一日内唯一），cancelOrder
   直接用它（比 QMT 的 seq↔real 解耦更简单——EMT 下单同步返真实 id）。

查询异步回调转 Future：EMT 的 queryPosition/queryAsset 是「请求→多次回调→last=True
结束」语义，与 QMT 同步返回不同。本网关用 reqid→Future+buffer 映射把回调聚合为
awaitable，超时兜底防永久挂起。

底层 API 事实来源：``emt_api_python/test/tradertest.py`` + 开发手册 §5.1（无幻觉）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Awaitable, Callable, Mapping, Optional

from trading.execution_gateway import BaseExecutionGateway, OrderRequest, OrderResult
from trading.order_state import OrderState

logger = logging.getLogger(__name__)

# === vnemttrader 延迟容错导入 ============================================
# Why add_dll_directory：Python 3.8+ Windows 不再从 PATH 查找扩展依赖 dll，
# 必须显式 os.add_dll_directory；否则 vnemttrader.pyd 加载报 DLL load failed。
_EMT_LIB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "emt_api_python", "lib", "windows",
)
if os.path.isdir(_EMT_LIB):
    if _EMT_LIB not in sys.path:
        sys.path.insert(0, _EMT_LIB)
    try:
        os.add_dll_directory(_EMT_LIB)
    except (OSError, AttributeError):  # pragma: no cover
        pass
try:
    from vnemttrader import TraderApi  # type: ignore
    _EMT_AVAILABLE = True
except ImportError:  # pragma: no cover - 环境相关，非逻辑分支
    TraderApi = None  # type: ignore[assignment]
    _EMT_AVAILABLE = False

# vnemttrader 缺失时退化为 object，维持类定义可加载（与 qmt_gateway 同款）
_CallbackBase = TraderApi if _EMT_AVAILABLE else object


# === EMT 枚举契约（来源开发手册 §5.1，与字面量一致防版本漂移）=============
# 市场
_EMT_MKT_SH_A = 2          # 上海A股
_EMT_MKT_SZ_A = 1          # 深圳A股
_EMT_MKT_BJ_A = 5          # 北京A股
# 买卖方向 / 价格 / 业务 / 开平
_EMT_SIDE_BUY = 1
_EMT_SIDE_SELL = 2
_EMT_PRICE_LIMIT = 1       # 限价单
_EMT_BUSINESS_NORMAL = 0   # 普通股票业务
_EMT_POSITION_OPEN = 1     # 开仓
# 报单状态 order_status（开发手册 §5.1.6）
_EMT_ORDER_ALLTRADED = 1       # 全部成交 → FILLED
_EMT_ORDER_PARTTRADED = 2      # 部分成交 → PARTIAL_FILLED
_EMT_ORDER_PARTCANCEL = 3      # 部分撤单 → PARTIAL_CANCELLED
_EMT_ORDER_NOTRADE = 4         # 未成交排队 → SUBMITTED
_EMT_ORDER_CANCELED = 5        # 已撤 → CANCELLED
_EMT_ORDER_REJECTED = 6        # 已拒绝 → REJECTED
_EMT_ORDER_UNKNOWN = 11        # 未知 → SUBMITTED（保守，不冒进终态）

# 上层注入的回报回调签名（与 qmt_gateway 一致）
OrderUpdateCallback = Callable[[Mapping[str, Any]], Awaitable[None]]


def _map_emt_status(status: int) -> OrderState:
    """EMT order_status 整数 → 内部 OrderState。

    风控语义：未到终态的（0/4/11 等）保守归 SUBMITTED，绝不因中间态/未知冒进成
    FILLED/REJECTED，未知状态由上层对账兜底（与 _map_qmt_status 同口径）。
    """
    if status == _EMT_ORDER_ALLTRADED:
        return OrderState.FILLED
    if status == _EMT_ORDER_PARTTRADED:
        return OrderState.PARTIAL_FILLED
    if status == _EMT_ORDER_PARTCANCEL:
        return OrderState.PARTIAL_CANCELLED
    if status == _EMT_ORDER_CANCELED:
        return OrderState.CANCELLED
    if status == _EMT_ORDER_REJECTED:
        return OrderState.REJECTED
    # 0/4/11/其他中间态统一视为「已提交」，等待后续回报推进
    return OrderState.SUBMITTED


def _split_symbol(symbol: str) -> tuple[str, int]:
    """内部标的 '600000.SH' → EMT ('600000', market=2)。后缀决定 market。

    Why 后缀映射：EMT 用纯数字 ticker + market 编码分离，而项目内部统一用带后缀
    的 symbol（与 xtquant/QMT 一致），故在网关边界做一次转换。
    """
    ticker, _, suffix = symbol.partition(".")
    suffix = suffix.upper()
    if suffix == "SH":
        return ticker, _EMT_MKT_SH_A
    if suffix == "SZ":
        return ticker, _EMT_MKT_SZ_A
    if suffix == "BJ":
        return ticker, _EMT_MKT_BJ_A
    raise ValueError(f"不支持的标的后缀：{symbol}（仅支持 .SH/.SZ/.BJ）")


class _EmtCallback(_CallbackBase):  # type: ignore[misc]
    """EMT 回调实现（运行在 C++ 线程）：解析 + 投递主线程，零阻塞零 State 直写。

    持有网关弱引用，所有 onXxx 只做 try-except 包裹的解析 + call_soon_threadsafe。
    """

    def __init__(self, gateway: "EmtExecutionGateway") -> None:
        if _EMT_AVAILABLE:
            super().__init__()
        self._gw = gateway

    # ---- 生命周期 ----
    def onDisconnected(self, reason):  # type: ignore[no-untyped-def]
        """连接断开（C++ 线程）：原子置锁 + 投递主线程告警。"""
        try:
            self._gw._lock_down = True
            self._gw._connected = False
            self._gw._loop.call_soon_threadsafe(self._gw._on_disconnect_fatal)  # type: ignore[union-attr]
        except Exception:
            logger.exception("EMT onDisconnected 异常，已吞并以保护 C++ 线程")

    # ---- 订单/成交推送 ----
    def onOrderEvent(self, data, error, session):  # type: ignore[no-untyped-def]
        """报单状态变动（C++ 线程）。"""
        try:
            status = data.get("order_status") if data else 0
            parsed = {
                "kind": "order",
                "order_emt_id": data.get("order_emt_id") if data else None,
                "ticker": data.get("ticker") if data else None,
                "order_status": status,
                "state": _map_emt_status(status),
                "qty_traded": (data or {}).get("qty_traded", 0),
                "qty_left": (data or {}).get("qty_left", 0),
                "price": (data or {}).get("price", 0.0),
                "side": (data or {}).get("side", 0),
                "error_id": (error or {}).get("error_id", 0),
                "error_msg": (error or {}).get("error_msg", ""),
            }
            self._gw._loop.call_soon_threadsafe(self._gw._process_order_update, parsed)  # type: ignore[union-attr]
        except Exception:
            logger.exception("EMT onOrderEvent 解析异常，已吞并")

    def onTradeEvent(self, data, session):  # type: ignore[no-untyped-def]
        """成交回报（C++ 线程）。"""
        try:
            parsed = {
                "kind": "trade",
                "order_emt_id": (data or {}).get("order_emt_id"),
                "ticker": (data or {}).get("ticker"),
                "price": (data or {}).get("price", 0.0),
                "quantity": (data or {}).get("quantity", 0),
                "trade_amount": (data or {}).get("trade_amount", 0.0),
                "state": OrderState.FILLED,
            }
            self._gw._loop.call_soon_threadsafe(self._gw._process_order_update, parsed)  # type: ignore[union-attr]
        except Exception:
            logger.exception("EMT onTradeEvent 解析异常，已吞并")

    def onCancelOrderError(self, data, error, session):  # type: ignore[no-untyped-def]
        """撤单失败（C++ 线程）。"""
        try:
            parsed = {
                "kind": "cancel_error",
                "order_emt_id": (data or {}).get("order_emt_id"),
                "error_id": (error or {}).get("error_id", -1),
                "error_msg": (error or {}).get("error_msg", ""),
                "state": OrderState.FAILED,
            }
            self._gw._loop.call_soon_threadsafe(self._gw._process_order_update, parsed)  # type: ignore[union-attr]
        except Exception:
            logger.exception("EMT onCancelOrderError 解析异常，已吞并")

    # ---- 查询回调（聚合到 reqid→buffer，last 时 resolve future）----
    def onQueryPosition(self, data, error, reqid, last, session):  # type: ignore[no-untyped-def]
        try:
            buf = self._gw._query_buffers.get(reqid)
            if buf is not None and data:
                buf.append({
                    "ticker": data.get("ticker"),
                    "market": data.get("market"),
                    "total_qty": data.get("total_qty", 0),
                    "sellable_qty": data.get("sellable_qty", 0),
                    "avg_price": data.get("avg_price", 0.0),
                })
            if last:
                self._gw._loop.call_soon_threadsafe(self._gw._resolve_query, reqid)  # type: ignore[union-attr]
        except Exception:
            logger.exception("EMT onQueryPosition 解析异常，已吞并")

    def onQueryAsset(self, data, error, reqid, last, session):  # type: ignore[no-untyped-def]
        try:
            buf = self._gw._query_buffers.get(reqid)
            if buf is not None and data:
                buf.append({
                    "total_asset": data.get("total_asset", 0.0),
                    "buying_power": data.get("buying_power", 0.0),
                    "withholding_amount": data.get("withholding_amount", 0.0),
                })
            if last:
                self._gw._loop.call_soon_threadsafe(self._gw._resolve_query, reqid)  # type: ignore[union-attr]
        except Exception:
            logger.exception("EMT onQueryAsset 解析异常，已吞并")


class EmtExecutionGateway(BaseExecutionGateway):
    """EMT 极速交易实盘网关（CTPAPI 风格：login + insertOrder + 回调）。"""

    def __init__(
        self,
        ip: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        client_id: Optional[int] = None,
        sock_type: Optional[int] = None,
        local_ip: Optional[str] = None,
    ) -> None:
        self._ip: str = ip or os.environ.get("EMT_IP", "")
        self._port: int = int(port or os.environ.get("EMT_PORT", "0"))
        self._user: str = user or os.environ.get("EMT_USER", "")
        self._password: str = password or os.environ.get("EMT_PASSWORD", "")
        self._client_id: int = int(client_id or os.environ.get("EMT_CLIENT_ID", "1"))
        self._sock_type: int = int(sock_type or os.environ.get("EMT_SOCK_TYPE", "1"))
        self._local_ip: str = local_ip or os.environ.get("EMT_LOCAL_IP", "127.0.0.1")
        if not (self._ip and self._port and self._user and self._password):
            raise ValueError(
                "缺少 EMT 凭证：请配置环境变量 EMT_IP/EMT_PORT/EMT_USER/EMT_PASSWORD，"
                "或在构造 EmtExecutionGateway 时传入"
            )

        # 运行态
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._api: Any = None          # _EmtCallback 实例（即 TraderApi）
        self._session: int = 0         # 0=未登录；login 成功得非 0 session
        self._connected: bool = False
        self._lock_down: bool = False  # 与 QmtExecutionGateway 同口径：初始 False，断线置 True

        # 订单回报流水（主线程独占）
        self._orders: dict[str, dict[str, Any]] = {}
        self._on_order_update: Optional[OrderUpdateCallback] = None

        # 查询异步回调聚合：reqid → (buffer_list, future)
        self._query_buffers: dict[int, list] = {}
        self._query_futures: dict[int, asyncio.Future] = {}
        self._reqid: int = 100         # 自增请求 id

    # ------------------------------------------------------------------ 连接
    async def connect(self) -> None:
        """login 建立连接（同步阻塞，投线程池）。

        时序遵循 tradertest.py：createTraderApi → subscribePublicTopic →
        setSoftwareVersion → login。session=0 即登录失败（getApiLastError 取原因）。
        """
        self._loop = asyncio.get_running_loop()
        if not _EMT_AVAILABLE:
            raise RuntimeError(
                "vnemttrader 不可用。EmtExecutionGateway 需 Python 3.10 + "
                "emt_api_python/lib/windows；开发/测试环境请用 MockExecutionGateway。"
            )

        def _bootstrap() -> tuple[Any, int]:
            api = _EmtCallback(self)
            api.createTraderApi(self._client_id, os.getcwd(), 4)
            api.subscribePublicTopic(2)  # EMT_TERT_QUICK：只传送登录后公共流
            api.setSoftwareVersion("quanter")
            session = api.login(
                self._ip, self._port, self._user,
                self._password, self._sock_type, self._local_ip,
            )
            return api, session

        try:
            api, session = await self._loop.run_in_executor(None, _bootstrap)
        except Exception as exc:
            self._lock_down = True
            raise ConnectionError(f"EMT login 异常：{exc}") from exc

        # _EmtCallback 实例即 TraderApi（createTraderApi 是 in-place 初始化，见 tradertest.py）
        self._api = api

        if session == 0:
            self._lock_down = True
            err = {}
            try:
                err = self._api.getApiLastError() if self._api else {}
            except Exception:
                pass
            raise ConnectionError(
                f"EMT login 失败（session=0）：{err}；请核对 EMT_IP/PORT/USER/PASSWORD"
            )
        self._session = session
        self._connected = True
        self._lock_down = False
        logger.info("EMT 登录成功 user=%s session=%s", self._user, session)

    async def disconnect(self) -> None:
        """logout（同步阻塞，投线程池）；无条件回锁防断开瞬间发单竞态。"""
        if self._api is not None and self._loop is not None and self._session:
            try:
                await self._loop.run_in_executor(
                    None, lambda: self._api.logout(self._session)
                )
            except Exception as exc:
                logger.warning("EMT logout 异常（忽略）：%s", exc)
        self._connected = False
        self._lock_down = True
        self._session = 0

    # ----------------------------------------------------------- 持仓对账
    async def _fetch_broker_positions(self) -> Mapping[str, float]:
        """queryPosition 异步回调 → 聚合为 {symbol: total_qty}（可卖持仓口径）。

        Why 用 sellable_qty 而非 total_qty：联调期对账口径是「可操作持仓」
        （与 QmtExecutionGateway 的 can_use_volume 同口径），过滤 T+1 冻结仓。
        """
        if self._loop is None or self._api is None or not self._session:
            raise RuntimeError("EMT 未登录，无法对账（请先 await connect()）")
        if self._lock_down:
            raise RuntimeError("EMT 已锁定（断线保护），拒绝对账以防脏读")

        reqid = self._next_reqid()
        raw = await self._run_query(reqid, lambda: self._api.queryPosition(self._session, reqid))
        cleaned: dict[str, float] = {}
        for row in raw:
            ticker = row.get("ticker")
            qty = row.get("sellable_qty", 0)
            if not ticker or not qty:
                continue
            market = row.get("market")
            suffix = {1: ".SZ", 2: ".SH", 5: ".BJ"}.get(market, "")
            cleaned[f"{ticker}{suffix}"] = float(qty)
        return cleaned

    async def _fetch_asset(self) -> dict[str, Any]:
        """queryAsset → 资产 dict（供 service.get_asset 调用，非基类抽象）。"""
        if self._loop is None or self._api is None or not self._session:
            raise RuntimeError("EMT 未登录")
        if self._lock_down:
            raise RuntimeError("EMT 已锁定，拒绝查询资产")
        reqid = self._next_reqid()
        rows = await self._run_query(reqid, lambda: self._api.queryAsset(self._session, reqid))
        if not rows:
            return {}
        a = rows[-1]  # 资产通常单条
        return {
            "account_id": self._user,
            "cash": float(a.get("buying_power", 0.0)),
            "total_asset": float(a.get("total_asset", 0.0)),
            "market_value": float(a.get("withholding_amount", 0.0)),
        }

    async def _run_query(self, reqid: int, trigger: Callable[[], int],
                         timeout: float = 10.0) -> list:
        """通用查询：发起请求 → 等回调聚合 → 返回 buffer。超时返已聚合部分。

        回调（_EmtCallback.onQueryXxx）把数据 append 到 _query_buffers[reqid]，
        last=True 时 call_soon_threadsafe(_resolve_query, reqid) 设 future result。
        """
        buf: list = []
        future: asyncio.Future = self._loop.create_future()  # type: ignore[union-attr]
        self._query_buffers[reqid] = buf
        self._query_futures[reqid] = future
        try:
            await self._loop.run_in_executor(None, trigger)  # type: ignore[union-attr]
            await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("EMT 查询 reqid=%s 超时（%ss），返已聚合 %s 条", reqid, timeout, len(buf))
        finally:
            self._query_buffers.pop(reqid, None)
            self._query_futures.pop(reqid, None)
        return buf

    def _resolve_query(self, reqid: int) -> None:
        """主线程：查询回调 last=True 时设 future result（由 call_soon_threadsafe 投递）。"""
        fut = self._query_futures.get(reqid)
        if fut is not None and not fut.done():
            buf = self._query_buffers.get(reqid, [])
            fut.set_result(buf)

    def _next_reqid(self) -> int:
        self._reqid += 1
        return self._reqid

    # -------------------------------------------------------------- 下单
    async def submit_order(self, order: OrderRequest) -> OrderResult:
        """insertOrder 异步下单（同步阻塞，投线程池）。

        order_dict 构造（tradertest.py + 手册 §5.1）：
        - side/price_type 由 OrderRequest.side/price 映射
        - price=None → 暂不支持市价（联调期限价为主；市价 price_type 需按市场选 2/3/4）
        - order_client_id 用作备注（透传 order.order_id，便于回报对账）
        """
        if self._loop is None or self._api is None or not self._session:
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED,
                               message="网关未登录，拒单")
        if self._lock_down:
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED,
                               message="网关已锁定（断线保护），禁止发单")
        if not self._connected:
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED,
                               message="未连接，拒单")

        try:
            ticker, market = _split_symbol(order.symbol)
        except ValueError as exc:
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED,
                               message=str(exc))

        side = _EMT_SIDE_BUY if order.side == "buy" else _EMT_SIDE_SELL
        price = float(order.price) if order.price is not None else 0.0
        if order.price is None:
            # 联调期待支持：市价需按市场选 price_type（沪 3/深 2/4），第一版强制限价
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED,
                               message="EMT 第一版仅支持限价单（price 必填）")

        order_dict = {
            "ticker": ticker,
            "market": market,
            "side": side,
            "price_type": _EMT_PRICE_LIMIT,
            "price": price,
            "quantity": int(order.qty),
            "business_type": _EMT_BUSINESS_NORMAL,
            "position_effect": _EMT_POSITION_OPEN,
            "ep_id": 0,
        }

        def _do_order():
            return self._api.insertOrder(order_dict, self._session)

        try:
            order_emt_id = await self._loop.run_in_executor(None, _do_order)
        except Exception as exc:
            logger.exception("EMT insertOrder 异常 symbol=%s", order.symbol)
            return OrderResult(order_id=order.order_id or "", state=OrderState.FAILED,
                               message=f"下单异常：{exc}")

        if not order_emt_id:  # 0=失败
            err = {}
            try:
                err = self._api.getApiLastError()
            except Exception:
                pass
            return OrderResult(order_id=order.order_id or "", state=OrderState.REJECTED,
                               message=f"EMT 拒单 order_emt_id=0：{err}")

        # EMT 直接返真实 order_emt_id（无需 QMT 的 seq↔real 解耦）
        return OrderResult(
            order_id=str(order_emt_id),
            state=OrderState.SUBMITTED,
            message="已提交，等待柜台回报",
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        """cancelOrder（撤单直接用 order_emt_id，比 QMT 简单）。"""
        if self._lock_down or not self._connected or not self._session:
            return OrderResult(order_id=order_id, state=OrderState.REJECTED,
                               message="网关未登录或已锁定，撤单失败")
        try:
            order_emt_id = int(order_id)
        except (TypeError, ValueError):
            return OrderResult(order_id=order_id, state=OrderState.REJECTED,
                               message=f"非法 order_emt_id：{order_id}")

        def _do_cancel():
            return self._api.cancelOrder(order_emt_id, self._session)

        try:
            rc = await self._loop.run_in_executor(None, _do_cancel)
        except Exception as exc:
            logger.exception("EMT cancelOrder 异常 order_id=%s", order_id)
            return OrderResult(order_id=order_id, state=OrderState.FAILED,
                               message=f"撤单异常：{exc}")

        if not rc:  # 0=失败
            err = {}
            try:
                err = self._api.getApiLastError()
            except Exception:
                pass
            return OrderResult(order_id=order_id, state=OrderState.FAILED,
                               message=f"撤单失败 rc={rc}：{err}")
        # 撤单指令已发出，最终状态以 onOrderEvent 推送的 CANCELLED 为准
        return OrderResult(order_id=order_id, state=OrderState.CANCELLED,
                           message="撤单指令已发出，等待回报确认")

    # ---------------------------------------------------- 回调注入与查询
    def set_order_update_callback(self, cb: OrderUpdateCallback) -> None:
        """注入上层异步回报回调（钉钉报警/State 持久化）。必须 async。"""
        self._on_order_update = cb

    @property
    def is_locked(self) -> bool:
        return self._lock_down

    def get_order(self, order_id: str) -> Optional[Mapping[str, Any]]:
        return self._orders.get(order_id)

    # ----------------------------------------------- 主线程处理（被投递）
    def _process_order_update(self, update: Mapping[str, Any]) -> None:
        """主线程：更新订单流水 + 触发上层异步回调（与 qmt_gateway 同款）。"""
        oid = str(update.get("order_emt_id") or update.get("order_id") or "")
        if oid:
            self._orders[oid] = dict(update)
        if self._on_order_update is not None:
            try:
                self._loop.create_task(self._on_order_update(update))  # type: ignore[union-attr]
            except RuntimeError:
                logger.warning("事件循环不可用，丢弃一次订单回报回调 oid=%s", oid)

    def _on_disconnect_fatal(self) -> None:
        """主线程：断线告警（由 onDisconnected 经 call_soon_threadsafe 投递）。"""
        logger.critical(
            "【EMT 断线】user=%s 网关已锁定，禁止后续发单！请人工检查后重新 await connect()",
            self._user,
        )
