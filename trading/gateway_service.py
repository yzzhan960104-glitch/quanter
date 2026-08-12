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

W1-A/T2（2026-08-12）：本模块自 presentation/server/services/trading_service.py
1:1 下沉至 trading 包内，更名为 gateway_service。物理意图：切断 trading→presentation
反向依赖——网关单例 + HTTP 业务（status/positions/emergency_halt/query_trades/...）
本属 trading 内部领域逻辑，原挂 presentation 层导致 trading/engine、phases、order_state、
io.orders 反查 presentation（违反分层：领域层不得依赖表现层）。下沉后所有 trading 内
调用方直 import trading.gateway_service，presentation 层（main/api/v1/ops/review_service）
与 broadcast 也改指本模块，源文件 presentation/server/services/trading_service.py 删除。
行为零变更：符号集逐字一致（仅删未使用的孤儿 import PROJECT_ROOT，无任何引用点）。
"""
from __future__ import annotations

import csv
import io
import logging
import os
from datetime import datetime
from typing import Optional

from infra.notifier import NotificationManager, fire_and_forget
from broker.base import OrderResult  # Layer2 阶段6 follow-up #4b：execution_gateway 垫片已删，直指 broker.base 真身
from trading import qmt_market_data
from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：execution_gateway 垫片已删，直指 compute.types 真身
from trading.compute.risk import check_order  # Layer2 阶段6：直指 functional core 真身（risk_shield 垫片已删）
# W1-A/T2-Task7：_mode 从 critical 顶部直 import（critical 是零下游耦合叶子模块 · 无环），
# 供 _submit wrapper 读进程级交易模式（dry_run/live）补 dry_run kw。原 engine._submit 下沉。
from trading.critical import _mode

logger = logging.getLogger(__name__)

# ============ 层级五·实盘可追溯性 ============
# SSoT Phase A · A4：原 CSV 写盘链路（record_live_trade → logs/live_trades.csv +
# LIVE_TRADE_LOG/LIVE_TRADE_COLUMNS）已整体退役。成交流水真相源平移到 state_store.fill
# 表（UNIQUE(order_id, traded_time) 天然幂等，08-04 事故根因修复），submit 审计事件
# 平移到 state_store.trade_event 表（A1 完成）。CSV 在重放/补推下重复 append 的缺陷
# 不复存在，消费端（query_trades/export/post_close 归因/digest）一律读 DB。
#
# 前端下载/导出 CSV 字段顺序契约（SSoT Phase A · A2 抽出 _EXPORT_COLUMNS）。
# Why 单独抽常量：原 export/query 引用 LIVE_TRADE_COLUMNS；A4 删 LIVE_TRADE_COLUMNS 后
# _EXPORT_COLUMNS 是唯一字段顺序源（字段顺序 = 前端 TradesPage/下载 CSV 契约红线，
# 不能随写盘退役而改），消费端零改动。
_EXPORT_COLUMNS = [
    "timestamp", "symbol", "direction", "shares", "price",
    "strategy", "rationale", "kind",
]

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
        # 层级五·持仓富化（SSoT Phase B · B2：归因从 position 表读，原内存字典已删）：
        # 附 strategy/entry_rationale（未登记则 None，前端显示 '—'）。归因与 qty/avg_price
        # 同行落 position 表，重启后随持仓行存活；SELL 平仓 apply_fill_to_position 归零
        # 删行 → 归因随行消失（断点-3 Resolution：position 行删除即归因消失）。
        # 成本/现价/盈亏%（Task12+）：avg_price/last_price/pnl_pct 透出供钉钉持仓播报 + 前端展示。
        # last_price 存「有效现价或 None」——NaN 在上方 if 已被 last!=last 拦截，此处再守一道。
        attr = _read_position_attribution(sym)
        result.append({
            "symbol": str(sym),
            "qty": qty,
            "avg_price": (float(avg) if avg is not None else None),
            "last_price": (float(last) if (last is not None and last == last) else None),
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "strategy": attr.get("strategy"),
            "entry_rationale": attr.get("entry_rationale"),
        })
    return result


def _read_position_attribution(symbol: str) -> dict:
    """从 position 表读单标的归因 {strategy, entry_rationale}（B2 后归因真相源是 DB）。

    Why 抽小函数：get_positions 循环内每标的查一次，封装便于异常软降级（DB 读失败
    不阻断持仓查询主路径——持仓 qty/avg_price 是真相，归因是衍生审计字段）。
    """
    try:
        from trading import state_store
        row = state_store.get_position(_resolve_account_id(), symbol)
        if row is None:
            return {}  # 持仓行不存在（broker 有持仓但 DB 无行——对账漂移场景，归因自然空）
        return {"strategy": row.get("strategy"), "entry_rationale": row.get("entry_rationale")}
    except Exception:
        # 软降级：归因读失败不应阻断 get_positions（持仓主路径优先），返空归因。
        logger.exception("读 position 归因失败 symbol=%s（软降级返空归因）", symbol)
        return {}


def record_position_attribution(symbol: str, strategy: str, rationale: str = "") -> None:
    """登记某标的的建仓策略与因子逻辑（B2：落 position 表 strategy/entry_rationale 列）。

    供实盘 BUY 成交回调（engine._handle_order_update 接线）调用：把「策略 + 入场因子逻辑」
    与标的绑定，使 Cockpit 持仓表能回答「这只票是哪个策略、因什么因子建的仓」。
    持仓行须已由 apply_fill_to_position 建立（BUY 成交必先建行），UPDATE 命中 1 行；
    SELL 平仓 apply_fill_to_position 归零删行，归因随行消失（不调 clear——断点-3 Resolution）。
    """
    from trading import state_store
    state_store.upsert_position_attribution(_resolve_account_id(), symbol, strategy, rationale)


def clear_position_attribution(symbol: str) -> None:
    """清除某标的的归因（B2：position 表 strategy/entry_rationale 置 NULL）。

    Note: B2 实际生产中**不调用本函数**——SELL 平仓 apply_fill_to_position 归零删
    position 行，归因随行消失。本函数保留供显式清除场景（人审纠错擦除但持仓留）+
    测试断言 upsert/clear 往返幂等。
    """
    from trading import state_store
    state_store.clear_position_attribution(_resolve_account_id(), symbol)


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
        # strategy 读 fill.strategy 列（A1 加，A3 SELECT 补）；rationale 仍空
        # （fill 表无此列，复盘按需回查 order 表）；kind='fill' 标注（DB fill 表本身就是
        # 成交回报，与 CSV 的 kind='fill' 等价）。
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
            # 读 fill.strategy 值（A1 加列，A3 SELECT 已返此字段）；保 _EXPORT_COLUMNS shape
            "strategy": r.get("strategy") or "",
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
            # 读 fill.strategy 值（A1 加列，A3 SELECT 已返此字段）
            "strategy": r.get("strategy") or "",
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
def _enforce_session() -> bool:
    return os.getenv("QMT_ENFORCE_SESSION", "true").lower() == "true"


def _in_a_share_session(now=None) -> bool:
    """粗略判断当前是否 A 股交易时段（09:15-15:00 连续，工作日）。

    A1（08-05 废单根治）：上午起点 09:30 → 09:15，含集合竞价——pre_open 调度在
    09:22 挂单，旧口径把自家调度时间当非法时段（300358 废单根因）。隔夜/周末保护
    仍保留（09:15 前、周末均拦）。
    D2 修订（2026-08-06 用户选项 1）：09:15-15:00 **连续**（午休 11:30-13:00 不拦）——
    柜台午休可接收排队单（08-06 实测直连可 SUBMITTED），引擎链路应同样允许。
    Why now 可注入：纯函数便于单测（避免 datetime.now() 不可控）。
    """
    from datetime import datetime
    now = now or datetime.now()
    if now.weekday() >= 5:  # 5=周六 6=周日
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= t <= 15 * 60


def _dry_run_direction(side: str) -> str:
    """dry_run 模拟的 direction 取值（落 CSV 审计）。"""
    return "DRY_RUN_BUY" if side.lower() == "buy" else "DRY_RUN_SELL"


# H3/T2 收口（2026-08-12）：单一真相源 trading/account.py，不再本地复制。
# 原本地实现避免 import engine 触发 server 启动期副作用；account 模块轻量（os + lazy
# state_store），import 它无 APScheduler/网关装配副作用，可直 import。
from trading.account import resolve_account_id as _resolve_account_id


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
    # date 统一带横线（YYYY-MM-DD，与 clock.today()/next_trading_day 返回一致）—— Fix1 rework：
    # 原传 ``.replace('-', '')``（无横线 20260805）与 engine（带横线 2026-08-05）不同 trade_id，
    # UNIQUE(account_id, trade_id, action) 不去重，A1 断点-1 双写幂等失效（server-manual 与
    # engine pre_open 同 symbol+date 各写一行 ORDERED 共存）。此处去掉 replace 恢复同构。
    # 注：server-manual 用当日 clock.today()，engine pre_open 用 next_trading_day（T+1 计划日），
    # 日期语义由调用方负责；build_trade_id 只管格式统一。
    trade_id = state_store.build_trade_id(aid, order.symbol, clock.today())
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


async def submit_order(order: OrderRequest, *, dry_run: bool) -> dict:
    """下单业务编排：风控挡板（A-2 三闸）→ 真单/模拟/拒单 → 落审计事件。

    返回：
    - dry_run 命中：{"order_id":"", "state":"DRY_RUN", "message":<reason>}（不真下单）
    - 真单成功：{"order_id":<seq-str>, "state":<OrderState.name>, "message":<...>}
    挡板命中（非 dry_run）：raise RuntimeError(reason)（路由层转 409）

    交易流水全覆盖（spec §6.3）：dry_run / BLOCKED / 真单 / 废单 / 撤单 均落 trade_event。

    A-2（2026-08-06 裁定 D1-D3/D5）：confirm/whitelist/quote 相关挡板已删除——confirm
    由计划确认闸承担，白名单前端同步放开（D3），涨跌停/金额/股数由柜台与交易所兜底。
    """
    gw = get_gateway()
    if gw is None:
        raise RuntimeError("交易网关未装配（unavailable）")

    # 1. 风控挡板（A-2 三闸短路：connection / dry_run / session）
    decision = check_order(
        order,
        dry_run=dry_run,
        enforce_session=_enforce_session(),
        is_locked=bool(getattr(gw, "is_locked", False)),
        connected=bool(getattr(gw, "_connected", False)),
        in_session=_in_a_share_session(),
    )

    # 2. 命中处理：落 trade_event 审计事件 + 返回/抛错
    # SSoT Phase A · Task A1：审计真相源从 CSV（record_live_trade）平移到 trade_event 表。
    # Why 平移：CSV 在重放/补推下重复 append（无 UNIQUE 约束），trade_event 表
    # UNIQUE(account_id, trade_id, action) 天然幂等，是审计事件的真相源。submit_order
    # 的四态（DRY_RUN/BLOCKED/ORDERED/REJECTED）全部落 trade_event，归因/复盘消费端切 DB。
    if decision.blocked:
        if decision.is_dry_run:
            # 模拟：落 DRY_RUN 事件后返回成功语义（非错误）
            # meta_kind=submit 显式传：防默认 "fill" 误标成交（DRY_RUN 是下单审计，
            # 与 BLOCKED/ORDERED 同语义，post_close 聚合按 kind=fill 闸识别真实成交）
            _write_submit_trade_event(
                order, "DRY_RUN", meta_reason=decision.reason, meta_kind="submit")
            return {"order_id": "", "state": "DRY_RUN", "message": decision.reason}
        # 真拒单：落 BLOCKED 事件 + raise（路由层转 409）
        _write_submit_trade_event(
            order, "BLOCKED",
            meta_reason=f"{decision.stage}:{decision.reason}",
            meta_kind="submit",  # 拦截/拒单是下单审计事件（非真实成交）
        )
        raise RuntimeError(decision.reason)

    # 3. 全过 → 真下单
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


async def _submit(order) -> dict:
    """下单 wrapper：补 dry_run=(_mode()=="dry_run")（原 engine._submit 下沉，gateway concern 收口）。

    物理意图（W1-A/T2-Task7 切断 engine 反查）：原 engine 模块级 ``_submit(order)`` 是
    pre_open / stop_loss / exit phases 的下单入口——单参 order，内部按进程级 ``_mode()``
    补 ``dry_run`` kw 调 submit_order。本函数把该 wrapper 下沉到 gateway_service（与
    submit_order / get_gateway 同住），phases 改顶部 ``from trading.gateway_service import _submit``
    直 import 物理真身，消除「engine 反查」中间层。

    行为等价红线：``_submit(order)`` 与原 ``engine._submit(order)`` 逐字等价——同样
    ``return await submit_order(order, dry_run=(_mode() == "dry_run"))``，dry_run 判定
    单点不变（_mode 是进程级开关，pre_open/stop_loss「影子即整批不真单」语义）。

    Why dry_run 用 _mode() 而非参数注入（沿用原 engine._submit 设计）：pre_open/stop_loss
    都是「影子即整批不真单」语义，_mode 是进程级开关，逐单传参反而引入「单只切 live」
    的误操作面。``patch("trading.engine._submit")`` 类测试因 phases 不再经 engine 而失效，
    Task 8-19 迁 patch 至 **phases 调用方模块**的 ``_submit``（如 ``trading.phases.pre_open._submit``）——
    因 phases ``from trading.gateway_service import _submit`` 为本地绑定，patch gateway_service
    只改模块属性、不命中 phases 本地引用，须 patch 调用方模块。
    """
    return await submit_order(order, dry_run=(_mode() == "dry_run"))


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
