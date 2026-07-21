# -*- coding: utf-8 -*-
"""实盘交易服务：QMT 网关单例装配 + status/positions/emergency_halt 业务逻辑。

设计红线（Why 这样切分）：
- 单例装配在模块级 lazy：get_gateway() 首次调用时读环境变量构造，缺凭证/无
  xtquant 返 None。不在 import 期构造（避免无 xtquant 机器 import 即崩）；不在
  lifespan 自动 connect（connect 是同步阻塞 C++ 调用，会拖慢启动；由 Cockpit
  视图或调度器按需 connect）。
- status 四态严格镜像网关：unavailable（无单例）/ disconnected（未 connect）/
  live（已连接）/ vetoed_by_risk（断线锁定）。前端心跳灯完全镜像，绝不虚假繁荣。
- emergency_halt 幂等：lock_down 一旦置位，重复调用不再重复撤单（避免对同一批
  未终态订单发多次撤单指令，防柜台风控误判）。

Why 模块级 import fire_and_forget：emergency_halt 投递告警走 fire_and_forget，
模块级暴露该名字便于测试 monkeypatch 屏蔽告警副作用（起 daemon thread）。
"""
from __future__ import annotations

import csv
import io
import logging
import os
from datetime import datetime
from typing import Optional

from core.notifier import NotificationManager, fire_and_forget
from server.core.config import PROJECT_ROOT
from trading import qmt_market_data
from trading.execution_gateway import OrderRequest, OrderResult
from trading.risk_shield import check_order

logger = logging.getLogger(__name__)

# ============ 层级五·实盘可追溯性 ============
# 实盘交易日志（CSV 持久化）：record_live_trade 追加，export_trades 按日期过滤读取。
# 设计意图（反黑盒）：CSV 是标准化、可审计、可被 Layer 6 LLM 复盘消费的格式；
# 落盘而非仅内存，进程重启后历史成交可追溯（实盘合规基线）。
LIVE_TRADE_LOG = os.path.join(str(PROJECT_ROOT), "logs", "live_trades.csv")
LIVE_TRADE_COLUMNS = [
    "timestamp", "symbol", "direction", "shares", "price", "strategy", "rationale",
]

# 持仓归因注册表（内存）：symbol → {strategy, rationale}。
# 实盘 submit_order 成交时调 record_position_attribution 登记；get_positions 据此富化。
# Why 内存而非落盘：持仓归因是「当前态」快照（平仓即清除），与成交日志（历史态）语义不同。
_position_attribution: dict = {}

# 模块级单例（lazy：首次 get_qmt_gateway 调用时构造）
_gateway_singleton: Optional[object] = None


def get_gateway() -> Optional[object]:
    """懒构造交易网关单例（Phase 1.5：EMT 优先，QMT 回退，都无则 None）。

    优先级：
    1. EMT 凭证（EMT_USER/EMT_PASSWORD）齐全 → EmtExecutionGateway
    2. QMT 凭证（QMT_USERDATA_PATH/QMT_ACCOUNT_ID）齐全 → QmtExecutionGateway
    3. 都无 → None（Cockpit 走 unavailable 降级态）

    Why 懒构造不在 import 期：EMT 的 vnemttrader / QMT 的 xtquant 都是 Windows C++
    扩展，开发机/CI 无相应包时 import 会触发 ImportError；放函数内 + try/except 让
    无 SDK 环境也能正常 import trading_service（仅 get_gateway 返 None）。
    """
    global _gateway_singleton
    if _gateway_singleton is not None:
        return _gateway_singleton
    # 优先 EMT（Phase 1.5 主用券商，MiniQMT 因监管停用后改用）
    if os.environ.get("EMT_USER") and os.environ.get("EMT_PASSWORD"):
        try:
            from trading.emt_gateway import EmtExecutionGateway
            _gateway_singleton = EmtExecutionGateway()
            logger.info("EMT 网关单例已构造（未 connect）user=%s",
                        os.environ.get("EMT_USER"))
            return _gateway_singleton
        except Exception as e:
            logger.warning("EMT 网关构造失败（无 vnemttrader?），尝试 QMT：%s", e)
    # 回退 QMT（Phase 1 既有，MiniQMT 监管可用时）
    if os.environ.get("QMT_USERDATA_PATH") and os.environ.get("QMT_ACCOUNT_ID"):
        try:
            from trading.qmt_gateway import QmtExecutionGateway
            _gateway_singleton = QmtExecutionGateway()
            logger.info("QMT 网关单例已构造（未 connect）account=%s",
                        os.environ.get("QMT_ACCOUNT_ID"))
            return _gateway_singleton
        except Exception as e:
            logger.warning("QMT 网关构造失败（无 xtquant?），走 unavailable：%s", e)
            return None
    logger.info("无 EMT/QMT 凭证，trading_service 走 unavailable 模式")
    return None


# 向后兼容别名（Phase 1 外部调用方/旧名引用）
get_qmt_gateway = get_gateway


def get_status() -> dict:
    """四态探测：unavailable / disconnected / live / vetoed_by_risk。

    锁定优先于连接：即便 _connected=True，只要 is_locked=True 即视为风控否决
    （断线瞬间 _connected 可能未被 on_disconnected 翻转，但 _lock_down 已率先置位）。
    """
    gw = get_gateway()
    if gw is None:
        return {"connected": False, "locked": False, "mode": "unavailable"}
    locked = bool(getattr(gw, "is_locked", False))
    connected = bool(getattr(gw, "_connected", False))
    if locked:
        return {"connected": connected, "locked": True, "mode": "vetoed_by_risk"}
    if connected:
        return {"connected": True, "locked": False, "mode": "live"}
    return {"connected": False, "locked": False, "mode": "disconnected"}


async def get_positions() -> list:
    """聚合底层真实持仓 → [{symbol, qty, market_value, pnl}]。

    pnl = market_value - open_cost（累计浮盈；XtPosition 不带昨收，无法算"今日"盈亏，
    务实口径见 spec 偏差记录）。第一版 market_value/pnl 走 None（未查行情，前端中性灰），
    仅返 symbol/qty，避免引入额外行情查询接口。
    未连接/锁定 → raise RuntimeError（路由层转 409）；无网关 → raise（路由层转 503）。
    """
    gw = get_gateway()
    if gw is None:
        raise RuntimeError("交易网关未装配（unavailable）")
    if getattr(gw, "is_locked", False) or not getattr(gw, "_connected", False):
        raise RuntimeError("交易网关未连接或已锁定，拒绝对账")
    raw = await gw._fetch_broker_positions()   # {stock_code: volume}
    if not raw:
        return []
    # 层级五·持仓富化：join 归因注册表，附 strategy/entry_rationale（未登记则 None，前端显示 '—'）。
    # market_value/pnl 仍 None（第一版未查行情）；契约形状就位，待行情查询接入后填充。
    return [
        {
            "symbol": str(sym),
            "qty": float(qty),
            "market_value": None,
            "pnl": None,
            "strategy": _position_attribution.get(sym, {}).get("strategy"),
            "entry_rationale": _position_attribution.get(sym, {}).get("rationale"),
        }
        for sym, qty in raw.items()
    ]


def record_position_attribution(symbol: str, strategy: str, rationale: str = "") -> None:
    """登记某标的的建仓策略与因子逻辑（供 get_positions 富化）。

    供实盘 submit_order 成交回调调用：把「策略 + 入场因子逻辑」与标的绑定，
    使 Cockpit 持仓表能回答「这只票是哪个策略、因什么因子建的仓」。
    平仓后应清除（调 clear_position_attribution）。
    """
    _position_attribution[symbol] = {"strategy": strategy, "rationale": rationale}


def clear_position_attribution(symbol: str) -> None:
    """清除某标的的归因（平仓后调用，防过期归因污染后续持仓）。"""
    _position_attribution.pop(symbol, None)


def record_live_trade(
    symbol: str,
    direction: str,
    shares: float,
    price: float,
    strategy: str = "",
    rationale: str = "",
) -> None:
    """追加一笔实盘成交到 logs/live_trades.csv（CSV 导出 + Layer 6 LLM 复盘数据源）。

    供实盘订单成交回调调用。文件不存在/空时先写表头；utf-8-sig 编码便于 Excel 直开。
    """
    os.makedirs(os.path.dirname(LIVE_TRADE_LOG), exist_ok=True)
    is_new = (not os.path.exists(LIVE_TRADE_LOG)) or os.path.getsize(LIVE_TRADE_LOG) == 0
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "direction": direction,
        "shares": shares,
        "price": price,
        "strategy": strategy,
        "rationale": rationale,
    }
    with open(LIVE_TRADE_LOG, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=LIVE_TRADE_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def export_trades(start: str, end: str) -> str:
    """按日期区间 [start, end]（YYYY-MM-DD）导出实盘成交 CSV 字符串。

    无日志 → 仅返回表头（诚实空导出，非 404；前端照常下载）。
    日期过滤按 timestamp 的日期前缀闭区间比较。
    """
    if not os.path.exists(LIVE_TRADE_LOG):
        return ",".join(LIVE_TRADE_COLUMNS) + "\n"
    rows = []
    with open(LIVE_TRADE_LOG, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ts = r.get("timestamp", "")
            day = ts.split(" ")[0]
            if start <= day <= end:
                rows.append(r)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LIVE_TRADE_COLUMNS)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in LIVE_TRADE_COLUMNS})
    return buf.getvalue()


def query_trades(
    start: str,
    end: str,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """分页查询实盘成交流水（logs/live_trades.csv）。

    过滤维度（AND 关系）：
    - 日期闭区间：按 timestamp 的日期前缀（YYYY-MM-DD）字典序比较，与 export_trades 同口径。
    - symbol：精确匹配（标的代码全字串，如 "510300.SH"）。
    - direction：大小写不敏感匹配（"buy" / "sell"），以兼容生产 CSV 的大写口径
      （record 流水落盘为 BUY/SELL/BLOCKED/DRY_RUN_*）。
    分页：limit/offset 在「过滤后全集」上切片；total 始终是过滤后命中总数（前端据此渲染分页器）。
    返回：{trades: [...], total: int, limit, offset}。
    降级：文件不存在 → 空结果（诚实空，不抛），与 export_trades 保持一致。

    Why limit 上限 1000：live_trades.csv 是单文件全表扫描（无索引），不设上限会被
    前端误传大 limit 拖垮（CSV 读取 + Python 行迭代在数万行级别已明显延迟）；
    1000 既覆盖看板单页可视上限，又给运维查最近一段留足空间。
    Why 数值字段 float 转换：CSV 原生皆 str，前端 TS 类型期望 shares/price 为 number；
    在服务端兜底转一次（转不动保留原串，让前端 parse 兜底而非整列空）。
    """
    # 入参兜底：limit 钳到 [1, 1000]，offset 钳到 >= 0（防前端传负/超大值）
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    if not os.path.exists(LIVE_TRADE_LOG):
        # 诚实空：文件尚未生成（未成交过）→ 直接返空，不抛 FileNotFoundError
        return {"trades": [], "total": 0, "limit": limit, "offset": offset}
    matched: list = []
    with open(LIVE_TRADE_LOG, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            # 日期闭区间：timestamp 形如 "2026-07-21 09:35:00"，取日期前缀字典序比较
            day = r.get("timestamp", "").split(" ")[0]
            if not (start <= day <= end):
                continue
            if symbol and r.get("symbol") != symbol:
                continue
            # direction 过滤：大小写不敏感。CSV 大写口径（BUY/SELL），
            # 前端/调用方传小写（"buy"/"sell"）亦能命中，避免前端 direction 过滤恒空。
            if direction and (r.get("direction") or "").lower() != direction.lower():
                continue
            # 数值字段尽力转 float：转不动（空串/脏数据）保留原串，前端兜底
            row = dict(r)
            for k in ("shares", "price"):
                try:
                    row[k] = float(row[k])
                except (TypeError, ValueError):
                    pass
            # direction 规范化为小写口径：CSV 落盘是大写（BUY/SELL/DRY_RUN_BUY/BLOCKED，
            # 见 record_live_trade / submit_order 写盘点），但前端 TradesTable 用
            # `row.direction === 'buy'` 小写精确匹配做买入红(danger)/卖出绿(success)着色。
            # 若不在服务端统一小写化，BUY 行会被前端判为「非 buy」→ 错挂 success（卖色·绿），
            # SELL 行才挂 danger（买色·红）—— 视觉警示与交易动作完全颠倒，是交易 UI 红线 bug。
            # 与 brief_trading 的 `_dir` lambda 一致，消费者（前端/告警/复盘）一律拿小写口径。
            row["direction"] = (row.get("direction") or "").lower()
            matched.append(row)
    total = len(matched)
    # 分页切片：在「过滤后全集」上做 offset/limit（total 仍是全集计数，前端按 total 渲染分页器）
    page = matched[offset: offset + limit]
    return {"trades": page, "total": total, "limit": limit, "offset": offset}


def emergency_halt() -> dict:
    """一键熔断：置 lock_down + 告警。幂等。

    幂等规则：lock_down 已为 True 时直接返"已处于熔断态"，不重复处理。
    Why 本期不主动撤单：撤所有未终态订单需遍历 _orders + 逐个 cancel_order（async），
    与同步 emergency_halt 语义冲突；本期仅置 lock_down（后续 submit_order 见此标志即
    拒，等效"停止一切新发单"的熔断语义）。撤单留待调度器单独触发。

    无网关 → raise RuntimeError（路由层转 503）。
    """
    gw = get_gateway()
    if gw is None:
        raise RuntimeError("交易网关未装配（unavailable），无法熔断")

    if getattr(gw, "_lock_down", False):
        return {"halted": True, "message": "已处于熔断态（lock_down 已置位，跳过重复处理）"}

    # 置断线锁定：后续 submit_order/cancel_order 见此标志即拒（既有网关契约）
    gw._lock_down = True   # type: ignore[attr-defined]
    try:
        gw._connected = False   # type: ignore[attr-defined]  # 熔断即视为不可发单
    except Exception:
        pass

    # 钉钉最高级别告警（fire_and_forget，失败不影响熔断语义）
    try:
        fire_and_forget(
            NotificationManager.get_default().notify_risk_event(
                "【紧急熔断】人工触发 emergency_halt，网关已锁定，禁止后续发单", "ERROR"
            )
        )
    except Exception as e:
        logger.warning("熔断告警投递失败（不影响熔断语义）：%s", e)

    logger.critical("【紧急熔断】已触发，网关锁定")
    return {"halted": True, "message": "熔断已触发：网关锁定，后续发单一律拒绝"}


# ============================================================================
# Phase 1 Task 5：env 风控配置读取 + 连接/下单/撤单/查询
# ============================================================================
# Why 函数而非模块级常量：便于测试 monkeypatch 覆盖（直改函数返回值，无需 setenv），
# 且 env 可在进程运行中被 reload，函数读取总能拿到最新值。
def _allow_live() -> bool:
    """实盘总闸 QMT_ALLOW_LIVE_TRADE（true 时才允许前端 dry_run=false 真下单）。"""
    return os.getenv("QMT_ALLOW_LIVE_TRADE", "false").lower() == "true"


def _whitelist() -> set:
    """标的白名单（逗号分隔 → set）。空配置 → 空集（一切标的被挡板拒）。"""
    raw = os.getenv("QMT_SYMBOL_WHITELIST", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _max_amount() -> float:
    return float(os.getenv("QMT_ORDER_MAX_AMOUNT", "1000"))


def _max_shares() -> float:
    return float(os.getenv("QMT_ORDER_MAX_SHARES", "100"))


def _enforce_session() -> bool:
    return os.getenv("QMT_ENFORCE_SESSION", "true").lower() == "true"


def _in_a_share_session() -> bool:
    """粗略判断当前是否 A 股交易时段（9:30-11:30 / 13:00-15:00，工作日）。

    Why 粗略：精确时段需考虑节假日/集合竞价/港股通差异；此处仅做基本盘挡板，
    避免隔夜/周末误下单。生产可替换为更精确的日历服务。
    """
    from datetime import datetime
    now = datetime.now()
    if now.weekday() >= 5:  # 5=周六 6=周日
        return False
    t = now.hour * 60 + now.minute
    morning = 9 * 60 + 30 <= t <= 11 * 60 + 30
    afternoon = 13 * 60 <= t <= 15 * 60
    return morning or afternoon


def _dry_run_direction(side: str) -> str:
    """dry_run 模拟的 direction 取值（落 CSV 审计）。"""
    return "DRY_RUN_BUY" if side.lower() == "buy" else "DRY_RUN_SELL"


async def connect_gateway() -> None:
    """触发网关连接（Cockpit /connect 调用）。

    网关未装配 → RuntimeError（路由层转 503）；connect 失败 → ConnectionError 上抛（转 503）。
    Why 不在 lifespan 自动 connect：connect 是同步阻塞 C++ 调用，按需触发更可控。
    """
    gw = get_gateway()
    if gw is None:
        raise RuntimeError("交易网关未装配（unavailable），请配置 EMT_USER/EMT_PASSWORD 或 QMT_USERDATA_PATH/QMT_ACCOUNT_ID")
    await gw.connect()


async def disconnect_gateway() -> None:
    """优雅断开网关。"""
    gw = get_gateway()
    if gw is None:
        return
    await gw.disconnect()


async def submit_order(order: OrderRequest, *, dry_run: bool, confirm: bool) -> dict:
    """下单业务编排：预取 quote → 风控挡板 → 真单/模拟/拒单 → 落流水。

    返回：
    - dry_run 命中：{"order_id":"", "state":"DRY_RUN", "message":<reason>}（不真下单）
    - 真单成功：{"order_id":<seq-str>, "state":<OrderState.name>, "message":<...>}
    挡板命中（非 dry_run）：raise RuntimeError(reason)（路由层转 409）

    交易流水全覆盖（spec §6.3）：dry_run / BLOCKED / 真单 / 废单 / 撤单 均落 CSV。
    """
    gw = get_gateway()
    if gw is None:
        raise RuntimeError("交易网关未装配（unavailable）")

    # 1. 预取行情（涨跌停关 + 金额估算用）；失败返 None，挡板跳过涨跌停关
    quote = await qmt_market_data.get_quote(order.symbol)

    # 2. 风控挡板（10 关短路）
    decision = check_order(
        order,
        dry_run=dry_run,
        allow_live=_allow_live(),
        whitelist=_whitelist(),
        max_amount=_max_amount(),
        max_shares=_max_shares(),
        quote=quote,
        enforce_session=_enforce_session(),
        is_locked=bool(getattr(gw, "is_locked", False)),
        connected=bool(getattr(gw, "_connected", False)),
        confirm=confirm,
        in_session=_in_a_share_session(),
    )

    # 3. 命中处理：落流水 + 返回/抛错
    if decision.blocked:
        if decision.is_dry_run:
            # 模拟：落 DRY_RUN 流水后返回成功语义（非错误）
            record_live_trade(
                order.symbol, _dry_run_direction(order.side),
                order.qty, order.price or 0.0,
                rationale=decision.reason,
            )
            return {"order_id": "", "state": "DRY_RUN", "message": decision.reason}
        # 真拒单：落 BLOCKED 流水 + raise（路由层转 409）
        record_live_trade(
            order.symbol, "BLOCKED", order.qty, order.price or 0.0,
            rationale=f"{decision.stage}:{decision.reason}",
        )
        raise RuntimeError(decision.reason)

    # 4. 全过 → 真下单
    result: OrderResult = await gw.submit_order(order)
    # 真单审计落盘（spec §6.3 可追溯性契约：真单/废单/撤单均落 CSV）。
    # Why 此前缺失：原实现拿到 OrderResult 直接 return，真实成交在
    # logs/live_trades.csv 完全缺失，进程崩溃后存在「真实已成交但系统不知情」的
    # 敞口黑洞，违反量化交易审计合规红线（B-6/应修项1）。
    # rationale 记录网关类名 + 真实 state + message，便于事后复盘/对账。
    direction = "BUY" if order.side.lower() == "buy" else "SELL"
    record_live_trade(
        order.symbol, direction, order.qty, order.price or 0.0,
        rationale=f"{gw.__class__.__name__}:{result.state.name}:{result.message}",
    )
    return {
        "order_id": result.order_id,
        "state": result.state.name,
        "message": result.message,
    }


async def cancel_order(order_id: str) -> dict:
    """撤单（透传网关）。"""
    gw = get_gateway()
    if gw is None:
        raise RuntimeError("交易网关未装配（unavailable）")
    result = await gw.cancel_order(order_id)
    return {"order_id": result.order_id, "state": result.state.name, "message": result.message}


async def get_orders() -> list:
    """查询本地缓存的订单回报流水（主线程同步读，转 list[dict]）。"""
    gw = get_gateway()
    if gw is None:
        return []
    orders = getattr(gw, "_orders", {}) or {}
    return [dict(v) for v in orders.values()]


async def get_asset() -> dict:
    """查询资金资产（现金/总资产/市值）。未连接或无网关 → 空字典。

    双网关适配：
    - EMT：gw._fetch_asset()（async，queryAsset 回调聚合）
    - QMT：gw._trader.query_stock_asset（同步 C++，投线程池）
    """
    gw = get_gateway()
    if gw is None:
        return {}
    if getattr(gw, "is_locked", False) or not getattr(gw, "_connected", False):
        return {}
    # EMT 网关：_fetch_asset（async，queryAsset 回调聚合）
    if hasattr(gw, "_fetch_asset"):
        try:
            return await gw._fetch_asset()
        except Exception as e:
            logger.warning("EMT query_asset 异常：%s", e)
            return {}
    # QMT 网关：query_stock_asset（同步 C++，投线程池）
    import asyncio
    loop = asyncio.get_running_loop()
    try:
        asset = await loop.run_in_executor(
            None, lambda: gw._trader.query_stock_asset(gw._account)
        )
    except Exception as e:
        logger.warning("query_stock_asset 异常：%s", e)
        return {}
    if asset is None:
        return {}
    return {
        "account_id": getattr(asset, "account_id", ""),
        "cash": float(getattr(asset, "cash", 0.0)),
        "total_asset": float(getattr(asset, "total_asset", 0.0)),
        "market_value": float(getattr(asset, "market_value", 0.0)),
    }
