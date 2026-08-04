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

from infra.notifier import NotificationManager, fire_and_forget
from presentation.server.http.config import PROJECT_ROOT
from broker.base import OrderResult  # Layer2 阶段6 follow-up #4b：execution_gateway 垫片已删，直指 broker.base 真身
from trading import qmt_market_data
from trading.dynamic_whitelist import get_effective_whitelist
from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：execution_gateway 垫片已删，直指 compute.types 真身
from trading.compute.risk import check_order  # Layer2 阶段6：直指 functional core 真身（risk_shield 垫片已删）

logger = logging.getLogger(__name__)

# ============ 层级五·实盘可追溯性 ============
# 实盘交易日志（CSV 持久化）：record_live_trade 追加，export_trades 按日期过滤读取。
# 设计意图（反黑盒）：CSV 是标准化、可审计、可被 Layer 6 LLM 复盘消费的格式；
# 落盘而非仅内存，进程重启后历史成交可追溯（实盘合规基线）。
LIVE_TRADE_LOG = os.path.join(str(PROJECT_ROOT), "logs", "live_trades.csv")
LIVE_TRADE_COLUMNS = [
    "timestamp", "symbol", "direction", "shares", "price", "strategy", "rationale", "kind",
]

# 前端下载/导出 CSV 字段顺序契约（SSoT Phase A · A2 抽出）。
# Why 单独抽常量：export/query 两函数都需要这个字段顺序定义，原都引用
# LIVE_TRADE_COLUMNS；A4 计划删 record_live_trade 写盘链路 + LIVE_TRADE_COLUMNS，
# 提前抽出 _EXPORT_COLUMNS 让 export/query 不依赖 LIVE_TRADE_COLUMNS，A4 删它时
# 消费端零改动（字段顺序 = 前端 TradesPage/下载 CSV 契约红线，不能随写盘退役而改）。
_EXPORT_COLUMNS = [
    "timestamp", "symbol", "direction", "shares", "price",
    "strategy", "rationale", "kind",
]

# 持仓归因注册表（内存）：symbol → {strategy, rationale}。
# 实盘 submit_order 成交时调 record_position_attribution 登记；get_positions 据此富化。
# Why 内存而非落盘：持仓归因是「当前态」快照（平仓即清除），与成交日志（历史态）语义不同。
_position_attribution: dict = {}

# 模块级单例（lazy：首次 get_qmt_gateway 调用时构造）
_gateway_singleton: Optional[object] = None


def get_gateway() -> Optional[object]:
    """懒构造交易网关单例（QMT 唯一在用券商）。

    优先级：
    1. QMT 凭证（QMT_USERDATA_PATH/QMT_ACCOUNT_ID）齐全 → QmtExecutionGateway
    2. 无凭证 → None（Cockpit 走 unavailable 降级态）

    Why 懒构造不在 import 期：QMT 的 xtquant 是 Windows C++ 扩展，开发机/CI
    无该包时 import 会触发 ImportError；放函数内 + try/except 让无 SDK 环境
    也能正常 import trading_service（仅 get_gateway 返 None）。
    """
    global _gateway_singleton
    if _gateway_singleton is not None:
        return _gateway_singleton
    # QMT（miniQMT，唯一在用券商）
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
    logger.info("无 QMT 凭证，trading_service 走 unavailable 模式")
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
    """聚合底层真实持仓 → [{symbol, qty, avg_price, last_price, market_value, pnl, pnl_pct, strategy, entry_rationale}]。

    avg_price = 持仓成本价（broker.avg_price）；last_price = 现价（行情 tick last_price）。
    pnl = (last_price - avg_price) × qty（累计浮盈；XtPosition 不带昨收，无法算"今日"
    盈亏，务实口径见 spec 偏差记录）。
    pnl_pct = (last_price - avg_price) / avg_price × 100（盈亏百分比，供钉钉持仓播报 + 前端列展示）。
    market_value = last_price × qty（按现价估值持仓总市值）。

    Task12 修 G6（原第一版 pnl/market_value 恒 None）：持仓查询后批量
    ``qmt_market_data.get_quotes(syms)`` 取现价逐仓算浮盈。**盲价防御红线**：
    现价缺失（行情源对该标的返 None / NaN）或 avg_price 缺失 → pnl/market_value 一律
    返 None，绝不拿前一收盘价或脏数据「猜」浮盈（量化交易审计合规红线：浮盈错估
    会误导风控阈值与研究员人审，宁可显式空值也不用不实数据填值）。

    未连接/锁定 → raise RuntimeError（路由层转 409）；无网关 → raise（路由层转 503）。
    """
    gw = get_gateway()
    if gw is None:
        raise RuntimeError("交易网关未装配（unavailable）")
    if getattr(gw, "is_locked", False) or not getattr(gw, "_connected", False):
        raise RuntimeError("交易网关未连接或已锁定，拒绝对账")
    # 全量口径（tradable_only=False）：展示须含 T+1 冻结仓（真实敞口 + 浮盈），不可滤。
    # 与 sync_positions 对账同口径；区别于 stop_loss 的可操作口径（io.fetch_positions 默认 True）。
    raw = await gw._fetch_broker_positions(tradable_only=False)
    # T7：raw 形态可能是 {sym: float}（Mock）或 {sym: {volume, avg_price, ...}}（QMT）。
    # 扁平化同时保留 avg_price（Task12：算浮盈必需 avg_price，早期扁平化只取 volume 丢了
    # avg_price，致 pnl 无从计算——此处补回）。形态统一为 {sym: {"volume":v, "avg_price":a}}，
    # 与 sync_positions 扁平化同型 isinstance 防御。
    if raw and isinstance(next(iter(raw.values()), None), dict):
        raw = {
            s: {
                "volume": p.get("volume", 0.0),
                "avg_price": p.get("avg_price"),
            }
            for s, p in raw.items()
        }
    else:
        # {sym: float} 形态（Mock 无 avg_price）→ 补 None 占位，保持下游统一访问
        raw = {s: {"volume": p, "avg_price": None} for s, p in (raw or {}).items()}
    if not raw:
        return []

    # Task12 · 批量取现价算浮盈（修 pnl/market_value=None G6）：
    # ``qmt_market_data.get_quotes`` 一次批量调 ``xtdata.get_full_tick(syms)``，比逐仓
    # 单查 N→1 次 C++ 调用（与 stop_loss_monitor 同口径优化）。返回 {sym: tick_dict | None}，
    # 缺失标的显式 None（盲价防御下游分支据此判 None）。
    syms = list(raw.keys())
    quotes: dict = {}
    if syms:
        try:
            quotes = await qmt_market_data.get_quotes(syms)
        except Exception:
            # 行情查询整体异常（xtdata 不可用/网络故障）→ 全 None 降级，绝不阻断持仓查询主路径
            # （持仓 symbol/qty 是真相，浮盈是衍生——宁可空 pnl 也不能让 Cockpit 持仓表整页 500）。
            logger.exception("取现价失败，pnl/market_value 将为 None（不猜价）")

    result = []
    for sym, pos in raw.items():
        qty = float(pos["volume"])
        avg = pos.get("avg_price")
        tick = quotes.get(sym)
        # tick 形态可能为 None（缺失）或 dict（含 last_price 等驼峰字段）
        last = tick.get("last_price") if isinstance(tick, dict) else None
        # 盲价防御三连：last 缺失(None) / last 是 NaN(last!=last) / avg 缺失 → 一律 None。
        # NaN 防御：xtdata 在停牌/异常 tick 时偶发返 float('nan')，NaN 参与运算会污染
        # 整列（NaN × 100 仍 NaN），必须在算术前拦截。
        if last is None or avg is None or last != last:
            market_value, pnl, pnl_pct = None, None, None
        else:
            market_value = float(last) * qty
            pnl = (float(last) - float(avg)) * qty
            # 盈亏百分比（供钉钉持仓播报「+N.N%」+ 前端列）：avg==0 除零防御（柜台成本恒>0，理论不会）
            pnl_pct = (float(last) - float(avg)) / float(avg) * 100 if float(avg) != 0 else None
        # 层级五·持仓富化：join 归因注册表，附 strategy/entry_rationale（未登记则 None，
        # 前端显示 '—'）。富化逻辑与 Task5 完全一致，本次仅补现价查询与 pnl 计算。
        # 成本/现价/盈亏%（Task12+）：avg_price/last_price/pnl_pct 透出供钉钉持仓播报 + 前端展示。
        # last_price 存「有效现价或 None」——NaN 在上方 if 已被 last!=last 拦截，此处再守一道。
        result.append({
            "symbol": str(sym),
            "qty": qty,
            "avg_price": (float(avg) if avg is not None else None),
            "last_price": (float(last) if (last is not None and last == last) else None),
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "strategy": _position_attribution.get(sym, {}).get("strategy"),
            "entry_rationale": _position_attribution.get(sym, {}).get("rationale"),
        })
    return result


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
    kind: str = "fill",  # "submit"=下单审计（含 REJECTED/FAILED）/"fill"=真实成交回报
) -> None:
    """追加一笔实盘记录到 logs/live_trades.csv（CSV 导出 + Layer 6 LLM 复盘数据源）。

    kind 区分（#3 修复）：post_close 聚合净持仓只认 kind='fill'，避免 submit 行
    （拒单/重单）混入致幻影持仓。submit 行仍落盘满足审计合规（spec §6.3）。
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
        "kind": kind,
    }
    with open(LIVE_TRADE_LOG, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=LIVE_TRADE_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)



def aggregate_fills_by_symbol(start: str, end: str) -> dict[str, float]:
    """聚合 [start, end]（YYYY-MM-DD 闭区间）内 BUY/SELL 净持仓（post_close 归因用）·DB-only。

    数据源（SSoT spec §2.4 + §3.3.4，A2 删 CSV 回退）：
        - 唯一数据源 state_store.fill 表（UNIQUE(order_id, traded_time) 天然去重，
          08-04 事故后的成交流水真相源）。原 env 驱动的 CSV 回退开关（A2 已退役）+
          DB 异常自动回退 CSV 镜像两路在 A2 已删（CSV 在重放场景下重复 append 会
          聚合出幻象持仓，违反 SSoT 红线）。

    物理意图（08-04 事故根因）：
        原实现全量流式读 CSV，在重放/补推场景下会把 24 行重复 BUY 100 聚合成 2400
        股幻影 → post_close 归因日志误报 drift。切 fill 表后，同笔成交只 1 行 →
        净 100，与 position_book 对齐，归因日志正确。

    降级纪律（A2 收紧）：
        DB 异常 → logger.exception + 返 {}（绝不静默回退 CSV 让消费端拿到幻象数据；
        归因日志诚实标记空，运维据告警跟进 DB 修复，而非用 CSV 假象掩盖）。

    返回 shape {symbol: net_float}（不变，post_close 归因契约）：
        BUY 为正、SELL 为负，按 symbol 累加。
    """
    from trading import state_store
    try:
        rows = state_store.query_fills(start, end)
    except Exception:
        # DB 异常不阻断归因流程，但绝不回退 CSV —— SSoT 红线：fill 表是唯一真相源，
        # CSV 在重放场景下会重复。诚实返 {} 让归因日志标记「读失败」，运维跟进修复。
        logger.exception("query_fills 读 DB 失败，aggregate 返空（不回退 CSV）")
        return {}
    net: dict[str, float] = {}
    for r in rows:
        sym = r.get("symbol")
        direction = (r.get("direction") or "").upper()  # query_fills 返小写，统一转大写匹配
        shares = r.get("shares")
        if not sym or direction not in ("BUY", "SELL") or shares is None:
            continue
        # BUY 累加 / SELL 累减 → 净持仓（与原 CSV 聚合口径一致，但数据源是去重后的 fill 表）
        net[sym] = net.get(sym, 0.0) + (
            float(shares) if direction == "BUY" else -float(shares))
    return net

def export_trades(start: str, end: str) -> str:
    """按日期区间 [start, end]（YYYY-MM-DD）导出实盘成交 CSV 字符串·DB-only。

    数据源（SSoT spec §2.4 + §3.3.2，A2 删 CSV 回退）：
        - 唯一数据源 state_store.fill 表（UNIQUE(order_id, traded_time) 天然去重，
          08-04 事故后的成交流水真相源）。原 env 驱动的 CSV 回退开关（A2 已退役）+
          DB 异常自动回退 CSV 镜像两路在 A2 已删（CSV 在重放场景下会重复，违反 SSoT 红线）。

    物理意图（08-04 事故根因）：
        原实现流式读 CSV，重放场景下导出 24 行重复 → Layer6 LLM 复盘输入污染
        （LLM 把 24 行当 24 笔分析）。切 fill 表后导出永远是真相源 1 行。

    字段顺序契约（A2 抽出 _EXPORT_COLUMNS）：
        export/query 格式化都用 _EXPORT_COLUMNS（与原 LIVE_TRADE_COLUMNS 同值同序，
        前端 TradesPage 下载 CSV 契约红线）。A4 删 LIVE_TRADE_COLUMNS 后
        _EXPORT_COLUMNS 是唯一字段顺序源，消费端零改动。

    降级纪律（A2 收紧）：
        DB 异常 → logger.exception + 返仅表头字符串（不抛、不回退 CSV；前端照常下载
        空导出，运维据告警跟进 DB 修复）。

    返回 shape：CSV 字符串（_EXPORT_COLUMNS 表头 + 0..N 数据行），前端下载契约红线。
    无日志/空 DB → 仅返回表头（诚实空导出，非 404；前端照常下载）。
    """
    from trading import state_store
    try:
        rows = state_store.query_fills(start, end)
    except Exception:
        # DB 异常不阻断导出，但绝不回退 CSV —— SSoT 红线：fill 表是唯一真相源，
        # CSV 在重放场景下会重复。诚实返仅表头让前端下载空导出，运维跟进修复。
        logger.exception("query_fills 读 DB 失败，export 返仅表头（不回退 CSV）")
        return ",".join(_EXPORT_COLUMNS) + "\n"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS)
    writer.writeheader()
    for r in rows:
        # DB 行 → _EXPORT_COLUMNS 行 shape（前端下载契约）
        # traded_time(YYYYMMDDHHMMSS) → timestamp("YYYY-MM-DD HH:MM:SS") 兼容前端展示；
        # direction 落大写口径（与原 CSV 写盘一致，消费者按需小写化）；
        # strategy/rationale 留空（fill 表不含这两列，复盘/审计按需回查 order 表）；
        # kind='fill' 标注（DB fill 表本身就是成交回报，与 CSV 的 kind='fill' 等价）。
        tt = str(r.get("traded_time") or "")
        ts = (
            f"{tt[0:4]}-{tt[4:6]}-{tt[6:8]} {tt[8:10]}:{tt[10:12]}:{tt[12:14]}"
            if len(tt) >= 14 else tt
        )
        writer.writerow({
            "timestamp": ts,
            "symbol": r.get("symbol", ""),
            "direction": (r.get("direction") or "").upper(),  # 落大写口径
            "shares": r.get("shares", ""),
            "price": r.get("price", ""),
            "strategy": "",
            "rationale": "",
            "kind": "fill",
        })
    return buf.getvalue()


def query_trades(
    start: str,
    end: str,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """分页查询实盘成交流水·DB-only（SSoT spec §2.4，A2 删 CSV 回退）。

    数据源（SSoT 红线，A2 收紧）：
        - 唯一数据源 state_store.fill 表（UNIQUE(order_id, traded_time) 天然去重，
          08-04 事故后的成交流水真相源）。原 env 驱动的 CSV 回退开关（A2 已退役）+
          DB 异常自动回退 CSV 镜像两路在 A2 已删（CSV 在重放场景下会重复，违反 SSoT 红线）。

    过滤维度（AND 关系，与原 CSV 读口同口径）：
    - 日期闭区间：query_fills 按 traded_time 前 8 位与 [start,end] 字典序比较。
    - symbol：精确匹配（标的代码全字串，如 "510300.SH"），透传给 query_fills。
    - direction：大小写不敏感匹配（"buy" / "sell"），query_fills 内部已统一大写匹配；
      消费端返小写口径（与前端着色、简报 _dir lambda 一致）。
    分页：limit/offset 在「过滤后全集」上切片；total 始终是过滤后命中总数（前端据此渲染分页器）。
    返回：{trades: [...], total: int, limit, offset}（shape 不变，前端 TradesPage 契约红线）。
    降级：DB 异常 → logger.exception + 空结果（诚实空，不抛、不回退 CSV）。

    Why limit 上限 1000：原 CSV 单文件全表扫描无索引、Python 行迭代在数万行已延迟；
    切 fill 表后 query_fills 走 SQLite 索引仍保留上限护栏（防前端误传大 limit + LIMIT
    在大结果集下仍占内存）。
    Why 数值字段 float 转换：消费端（前端 TS）期望 shares/price 为 number；DB 取出
    已是 float（query_fills 返 dict 强转），这里再 float() 兜底（NULL/异常值安全）。

    消费端防御层（保持不变）：direction not in ("BUY","SELL") 的行（如 BLOCKED/
    DRY_RUN_*）在聚合层 aggregate_fills_by_symbol / 简报 _dir 都会被过滤，本函数
    只做日期/symbol/direction 三维过滤，不替消费端决定成交口径（kind 闸在 brief_trading）。
    """
    # 入参兜底：limit 钳到 [1, 1000]，offset 钳到 >= 0（防前端传负/超大值）
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))

    from trading import state_store
    try:
        rows = state_store.query_fills(start, end, symbol=symbol, direction=direction)
    except Exception:
        # DB 异常不阻断消费端展示，但绝不回退 CSV —— SSoT 红线：fill 表是唯一真相源，
        # CSV 在重放场景下会重复。诚实返空让前端展示「无成交」，运维据告警跟进修复。
        logger.exception("query_fills 读 DB 失败，query_trades 返空（不回退 CSV）")
        return {"trades": [], "total": 0, "limit": limit, "offset": offset}

    # normalize 成前端契约 shape（字段对齐 _EXPORT_COLUMNS，但 DB 行只含成交四要素 +
    # traded_time）。
    # traded_time(YYYYMMDDHHMMSS) → timestamp("YYYY-MM-DD HH:MM:SS") 兼容前端展示；
    # kind='fill' 标注（DB fill 表本身就是成交回报，与 CSV 的 kind='fill' 行等价）。
    matched: list = []
    for r in rows:
        tt = str(r.get("traded_time") or "")
        # traded_time "20260805101000" → "2026-08-05 10:10:00"（前端 TradeRecord.timestamp）
        ts = (
            f"{tt[0:4]}-{tt[4:6]}-{tt[6:8]} {tt[8:10]}:{tt[10:12]}:{tt[12:14]}"
            if len(tt) >= 14 else tt
        )
        matched.append({
            "timestamp": ts,
            "traded_time": tt,
            "symbol": r.get("symbol", ""),
            "direction": (r.get("direction") or "").lower(),  # 小写口径（query_fills 已小写，兜底）
            "shares": float(r.get("shares") or 0.0),
            "price": float(r.get("price") or 0.0),
            "strategy": "",
            "rationale": "",
            # DB fill 表就是成交回报 → kind='fill'（brief/聚合按 kind 闸识别）
            "kind": "fill",
            "order_id": r.get("order_id", ""),
        })
    total = len(matched)
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

    # 置风控熔断粘滞锁（#6：health_guard 不得自动重连解除）
    if hasattr(gw, "set_risk_halt"):
        gw.set_risk_halt(True)   # 置 _risk_halted=True + _lock_down=True + _connected=False
    else:
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
    """标的白名单（静态 env ∪ engine 动态注入）。

    Why 改造前是纯 env 解析，现在委托 dynamic_whitelist.get_effective_whitelist()：
    二期 engine 在 pre_open 把当日颈线法扫出的标的（创板/科创个股）临时注入白名单，
    才能过 risk_shield 关5；盘后 post_close 清空。详见 trading/dynamic_whitelist.py。

    跨进程语义：engine 与 server 是独立进程，_DYNAMIC 模块级全局只在 engine 进程内
    有效——server（手动下单）进程内 _DYNAMIC 恒空，返回值 = 纯 env，与改造前完全等价
    （向后兼容，前端手动下单语义不被 engine 内部状态污染）。
    空配置 → 空集（一切标的被挡板拒）。
    """
    return get_effective_whitelist()


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


def _resolve_account_id() -> str:
    """submit_order 写 trade_event 用的 account_id（UNIQUE 键之一）。

    与 engine._resolve_account_id:455 同口径（QMT_ACCOUNT_ID env 优先，缺失走
    state_store._DEFAULT_ACCOUNT_ID）——真正引用常量，engine 改 _DEFAULT_ACCOUNT_ID 时
    本函数自动跟。Why 本地实现而非 import engine._resolve_account_id：trading_service
    在 server 进程内 import engine 会触发 APScheduler/网关装配等副作用（engine 模块顶层
    有重型 import），且 server 与 engine 在 C-7 后合并进同进程但两套 _resolve_account_id
    各管各的下单路径（server 手动 vs engine 自动），保持独立函数便于测试 monkeypatch 隔离。
    改口径必须两处同步（注释锁，参见 [[gateway-ssot-hardening]]）。
    """
    from trading import state_store  # lazy import：避免 server 启动期 import 副作用
    return os.getenv("QMT_ACCOUNT_ID") or state_store._DEFAULT_ACCOUNT_ID


async def connect_gateway() -> None:
    """触发网关连接（Cockpit /connect 调用）。

    网关未装配 → RuntimeError（路由层转 503）；connect 失败 → ConnectionError 上抛（转 503）。
    Why 不在 lifespan 自动 connect：connect 是同步阻塞 C++ 调用，按需触发更可控。
    """
    gw = get_gateway()
    if gw is None:
        raise RuntimeError("交易网关未装配（unavailable），请配置 QMT_USERDATA_PATH/QMT_ACCOUNT_ID")
    await gw.connect()


async def disconnect_gateway() -> None:
    """优雅断开网关。"""
    gw = get_gateway()
    if gw is None:
        return
    await gw.disconnect()


def _write_submit_trade_event(
    order: OrderRequest,
    action: str,
    *,
    order_id: str | None = None,
    meta_reason: str = "",
    meta_kind: str = "fill",
    meta_direction: str | None = None,
) -> None:
    """submit_order 审计事件落 trade_event 表（SSoT Phase A · Task A1 平移）。

    物理意图：把 submit_order 四态（DRY_RUN/BLOCKED/ORDERED/REJECTED）从 CSV 镜像
    平移到 trade_event 表真相源（UNIQUE(account_id, trade_id, action) 天然幂等）。
    trade_id 用 build_trade_id(account_id, symbol, clock.today()) 单点构造（与
    engine ORDERED 事件 + eod_plan trade_id 同口径单点，改口径必须同步 [[gateway-ssot-hardening]]）。

    断点-1 双写幂等（action=ORDERED 的设计根源）：engine pre_open 自动下单路径与本函数
    server 手动下单路径**共用 action=ORDERED**——同 trade_id 时 UNIQUE(account_id, trade_id, action)
    让二次写 IntegrityError 返 False 自然跳过（双写安全）。若把真单 action 改成 result.state.name
    （SUBMITTED），engine 写 ORDERED、server 写 SUBMITTED 两行共存，幂等失效，消费端查「所有下单」
    需 IN ('ORDERED','SUBMITTED') 双值口子——破坏审计真相源的单值契约。真实 OrderState 通过
    meta_reason 携带保留。

    软降级（spec §6.3）：DB 写失败不阻断下单主路径——审计旁路仅 WARN，业务结果（网关
    返回值）优先返回。Why：下单已成功后 DB 写失败若 raise，会让用户看到 500 但实际订单
    已挂出，敞口更危险；审计缺失通过 _alert_critical 旁路告警人工补对账。

    meta 携带的字段（事件流的事后归因线索）：
    - reason：风控拒因 / 网关 state + message（便于事后复盘「为什么被拒/挂了什么状态」）
    - kind：submit（下单审计）/ fill（真实成交）—— 与原 CSV record_live_trade kind 同语义，
      post_close 聚合只认 fill，submit 行不计入净持仓（与原 CSV kind 口径一致）
    - direction：真单路径补 BUY/SELL（dry_run/BLOCKED 无 direction，由 action 间接表达）
    """
    from trading import state_store, clock  # lazy import：避免 server 启动期 import 副作用
    aid = _resolve_account_id()
    trade_id = state_store.build_trade_id(aid, order.symbol, clock.today().replace("-", ""))
    meta_parts = [f"reason={meta_reason}"] if meta_reason else []
    meta_parts.append(f"kind={meta_kind}")
    if meta_direction is not None:
        meta_parts.append(f"direction={meta_direction}")
    meta = "|".join(meta_parts)
    try:
        state_store.insert_trade_event(
            aid, trade_id, order.symbol, action,
            order_id=order_id, qty=float(order.qty), price=float(order.price or 0.0),
            meta=meta,
        )
    except Exception:
        # 审计旁路：DB 写失败不阻断下单（业务结果优先），告警 + 日志供人工补对账。
        logger.exception("submit_order trade_event 写失败 action=%s symbol=%s", action, order.symbol)


async def submit_order(order: OrderRequest, *, dry_run: bool, confirm: bool,
                       whitelist: set | None = None) -> dict:
    """下单业务编排：预取 quote → 风控挡板 → 真单/模拟/拒单 → 落审计事件。

    返回：
    - dry_run 命中：{"order_id":"", "state":"DRY_RUN", "message":<reason>}（不真下单）
    - 真单成功：{"order_id":<seq-str>, "state":<OrderState.name>, "message":<...>}
    挡板命中（非 dry_run）：raise RuntimeError(reason)（路由层转 409）

    交易流水全覆盖（spec §6.3）：dry_run / BLOCKED / 真单 / 废单 / 撤单 均落 CSV。

    ``whitelist`` 参数（C-2 scheduling-orchestration W1 物理隔离）：
    - server 手动下单路径不传（默认 None）→ 走 ``_whitelist()`` = get_effective_whitelist()
      （_DYNAMIC 恒空 = 纯 env，向后兼容不变）。
    - engine 自动下单通道显式传 ``self._dynamic_whitelist | static_env_whitelist()``
      （来自 engine 实例属性，不读模块全局 _DYNAMIC）——engine 与 server 合并进同进程后，
      两端白名单靠参数显式隔离，不再共享模块级全局态。
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
        whitelist=whitelist if whitelist is not None else _whitelist(),
        max_amount=_max_amount(),
        max_shares=_max_shares(),
        quote=quote,
        enforce_session=_enforce_session(),
        is_locked=bool(getattr(gw, "is_locked", False)),
        connected=bool(getattr(gw, "_connected", False)),
        confirm=confirm,
        in_session=_in_a_share_session(),
    )

    # 3. 命中处理：落 trade_event 审计事件 + 返回/抛错
    # SSoT Phase A · Task A1：审计真相源从 CSV（record_live_trade）平移到 trade_event 表。
    # Why 平移：CSV 在重放/补推下重复 append（无 UNIQUE 约束），trade_event 表
    # UNIQUE(account_id, trade_id, action) 天然幂等，是审计事件的真相源。submit_order
    # 的四态（DRY_RUN/BLOCKED/ORDERED/REJECTED）全部落 trade_event，归因/复盘消费端切 DB。
    if decision.blocked:
        if decision.is_dry_run:
            # 模拟：落 DRY_RUN 事件后返回成功语义（非错误）
            _write_submit_trade_event(order, "DRY_RUN", meta_reason=decision.reason)
            return {"order_id": "", "state": "DRY_RUN", "message": decision.reason}
        # 真拒单：落 BLOCKED 事件 + raise（路由层转 409）
        _write_submit_trade_event(
            order, "BLOCKED",
            meta_reason=f"{decision.stage}:{decision.reason}",
            meta_kind="submit",  # 拦截/拒单是下单审计事件（非真实成交）
        )
        raise RuntimeError(decision.reason)

    # 4. 全过 → 真下单
    result: OrderResult = await gw.submit_order(order)
    # 真单审计落 trade_event（spec §6.3 可追溯性契约：真单/废单/撤单均落审计）。
    # Why 此前缺失：原实现拿到 OrderResult 直接 return，真实成交在审计流水中完全缺失，
    # 进程崩溃后存在「真实已成交但系统不知情」的敞口黑洞，违反量化交易审计合规红线
    # （B-6/应修项1）。A1 平移后落 trade_event（真相源，UNIQUE 幂等），归因消费端切 DB。
    # action 统一用 ORDERED/REJECTED（非 result.state.name 如 SUBMITTED/FILLED）——这是
    # 断点-1 的「双写幂等」设计：engine pre_open 自动下单与 server 手动下单两路径共用
    # action=ORDERED，UNIQUE(account_id, trade_id, action) 让同 trade_id 二次写 IntegrityError
    # 返 False 自然跳过（engine 已写则 server 不重写；反之亦然）。若改用 result.state.name，
    # engine 写 ORDERED、server 写 SUBMITTED，两行共存 UNIQUE 去重失效，消费端查「所有下单」
    # 需 IN ('ORDERED','SUBMITTED') 双值口子——破坏审计真相源的单值契约。真实 OrderState
    # （SUBMITTED/FILLED/REJECTED/FAILED）通过 meta_reason 携带，事后复盘不丢信息。
    direction = "BUY" if order.side.lower() == "buy" else "SELL"
    _action = "ORDERED" if result.state.name not in ("REJECTED", "FAILED") else "REJECTED"
    _write_submit_trade_event(
        order, _action,
        order_id=result.order_id,
        meta_reason=f"{gw.__class__.__name__}:{result.state.name}:{result.message}",
        meta_kind="submit",  # 下单审计事件（含 REJECTED/FAILED），post_close 不计入净持仓
        meta_direction=direction,
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

    QMT 网关：gw._trader.query_stock_asset（同步 C++，投线程池）。
    """
    gw = get_gateway()
    if gw is None:
        return {}
    if getattr(gw, "is_locked", False) or not getattr(gw, "_connected", False):
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


# ============================================================================
# Phase 2 · 作业驾驶舱：get_jobs 聚合当天 job 台账 + 启动补跑 catchup 四态
# ============================================================================
def _resolve_catchup_state(task) -> dict:
    """把启动补跑 asyncio.Task 探测为 {state, result}（spec §5.2 catchup 四态）。

    纯函数，仅调 task.done()/exception()/result()（duck typing）——不依赖 asyncio
    事件循环，故测试可用 fake 对象注入（无需起真 loop，保持单测极速且无副作用）。
    状态分支：
    - task is None → not_started（lifespan 未起 catchup task，如冷启动首帧）
    - 未 done → running（asyncio.create_task 派发后、未完成的窗口）
    - exception 非 None → failed，result={'error': str(exc)}（前端可直显失败原因）
    - 否则 → done，result=run_startup_catchup 真实返回的 dict（pipeline/brief/pre_open/...）

    Why 不缓存 task 引用做轮询：本函数在 GET /trading/jobs 每次请求时被调用，
    现场探测一次即返——状态推进由 asyncio 自身完成，本函数无状态、无副作用。
    """
    if task is None:
        return {"state": "not_started", "result": None}
    if not task.done():
        return {"state": "running", "result": None}
    exc = task.exception()
    if exc is not None:
        return {"state": "failed", "result": {"error": str(exc)}}
    return {"state": "done", "result": task.result()}


def get_jobs(date: str, engine, catchup_task) -> dict:
    """聚合当天 job 台账 + 启动补跑 task 状态（GET /trading/jobs 消费，spec §5.1）。

    返回 {"date","jobs":[...],"catchup":{"state","result"},"warning"?}：
    - jobs：job_ledger.snapshot_for_date(date) 当天台账最新行（C-8 引入）
    - catchup：_resolve_catchup_state(catchup_task) 启动补跑四态
    - warning：仅台账读失败时出现（job 台账是操作元数据，绝不阻断观测主路径——
      即便 SQLite 被锁/文件损坏，前端驾驶舱仍要看见 catchup 状态，避免盲区）

    Why `from trading import job_ledger` 写在函数体内而非模块顶：测试需 monkeypatch
    job_ledger.snapshot_for_date，函数体内 import 在调用时取最新引用——让 patch
    一定能命中（若在模块顶 import，monkeypatch 改的是 trading.job_ledger 上的属性，
    本模块的本地引用会"漏过"patch，这是 Python import 语义的经典坑）。

    engine 参数当前未用：预留未来读 engine 内嵌可观测态（如当前持仓/挂单数），
    router 从 app.state 传入；本期 get_jobs 保持纯聚合层，不引入对 engine 内部结构的
    耦合。本函数无状态，全部信息来自入参与 job_ledger。
    """
    from trading import job_ledger
    out: dict = {"date": date, "jobs": [],
                 "catchup": {"state": "not_started", "result": None}}
    try:
        out["jobs"] = job_ledger.snapshot_for_date(date)
    except Exception as e:
        # 台账读失败降级：jobs=[] + warning，logger.exception 打全栈便于事后排查
        # （"db locked" 等偶发故障不阻断观测，但要在日志里留痕给运维）
        logger.exception("job 台账读取失败（GET /jobs 降级返空，不阻断观测）")
        out["jobs"] = []
        out["warning"] = f"job 台账读取失败：{e}"
    out["catchup"] = _resolve_catchup_state(catchup_task)
    return out
