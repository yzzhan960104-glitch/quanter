"""
trading/qmt_market_data.py
==========================
xtdata 行情封装（延迟容错）。

职责：提供单标的实时快照（last_price / high_limit / low_limit），供
- risk_shield 第 9 关（涨跌停封板校验）
- trading_service.get_positions（持仓市值/浮盈富化）

设计（CLAUDE.md 彻底掌控执行环境）：
- xtdata.get_full_tick 是同步 C++ 调用，经 loop.run_in_executor 投线程池，绝不阻塞事件循环。
- 延迟容错 import：无 xtquant 的开发/CI 环境 _XTDATA_AVAILABLE=False，get_quote 返 None，
  调用方据此降级（risk_shield 跳过涨跌停关，positions 市值为 None）。
- 任何异常捕获返 None，绝不冒泡——行情缺失不应阻断下单/查询主路径。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

try:
    from xtquant import xtdata  # type: ignore
    _XTDATA_AVAILABLE = True
except ImportError:  # pragma: no cover - 环境相关，非逻辑分支
    xtdata = None  # type: ignore[assignment]
    _XTDATA_AVAILABLE = False


# === xtquant 字段口径订正（实盘联调 2026-07-22 抓出）===========================
# xtdata.get_full_tick 返回的是【驼峰】字段：lastPrice/open/high/low/lastClose/askPrice...，
# 且【不含涨跌停】。原实现直接透传 raw tick，下游 risk_shield/engine 查下划线字段名
# (last_price/high_limit/low_limit) 全得 None —— 导致涨跌停封板校验永远跳过（风控失效）、
# 移动止损现价检查永远跳过。这里集中归一化：驼峰→下划线 + 补涨跌停（instrument_detail）。
_LIMIT_PRICE_CACHE: dict[str, tuple[str, "float", "float"]] = {}
# symbol -> (yyyymmdd, high_limit, low_limit)。涨跌停当日不变，按日缓存避免每 tick 重取。


def _fetch_limit_prices_sync(symbols: list[str]) -> dict[str, tuple["float | None", "float | None"]]:
    """同步批量取涨跌停价（xtdata.get_instrument_detail 单只，按日缓存）。

    Why 缓存：涨跌停由昨收×涨跌幅定，当日恒定；盘中 stop_loss 每 5min 巡查 N 只，
    缓存命中后 0 次 C++ 调用，仅冷启动首巡 N 次（投线程池不阻塞事件循环）。

    Returns:
        ``{symbol: (high_limit, low_limit)}``，取数失败/无值 → ``(None, None)``。
    """
    import datetime as _dt
    today = _dt.date.today().strftime("%Y%m%d")
    out: dict[str, tuple["float | None", "float | None"]] = {}
    miss: list[str] = []
    for s in symbols:
        cached = _LIMIT_PRICE_CACHE.get(s)
        if cached and cached[0] == today:
            out[s] = (cached[1], cached[2])
        else:
            miss.append(s)
    for s in miss:
        try:
            detail = xtdata.get_instrument_detail(s) or {}  # type: ignore[union-attr]
            up = detail.get("UpStopPrice")
            dn = detail.get("DownStopPrice")
            hi = float(up) if up not in (None, "") else None
            lo = float(dn) if dn not in (None, "") else None
            if hi is not None and lo is not None:
                _LIMIT_PRICE_CACHE[s] = (today, hi, lo)
            out[s] = (hi, lo)
        except Exception:  # 单标的取数失败不阻断：该标的涨跌停降级 None
            out[s] = (None, None)
    return out


def _normalize_tick_sync(
    tick: "Any",
    high_limit: "float | None",
    low_limit: "float | None",
) -> "Optional[Mapping[str, Any]]":
    """把 xtdata 驼峰 tick 归一化成下游契约的下划线字段 + 注入涨跌停。

    防护（Grill Me 极端行情边界）：
    - ``lastPrice=0``（未订阅/集合竞价前/停牌）必须降级为 ``last_price=None``，
      否则 risk_shield 关9 会把 0≤跌停价误判为「跌停封板」错挡所有 SELL；
    - 原始 tick 非 dict / 缺失 → 返 None（让调用方按 quote=None 降级跳过）。
    """
    if not isinstance(tick, Mapping):
        return None
    lp = tick.get("lastPrice")
    # 价格必须 >0 才可信；0/None/NaN 一律降级 None（下游 engine 显式跳过，不发盲单）
    last_price = lp if (isinstance(lp, (int, float)) and lp > 0) else None
    return {
        "last_price": last_price,
        "open": tick.get("open"),
        "high": tick.get("high"),
        "low": tick.get("low"),
        "pre_close": tick.get("lastClose"),
        "volume": tick.get("volume"),
        "amount": tick.get("amount"),
        "high_limit": high_limit,
        "low_limit": low_limit,
        "ask_price": tick.get("askPrice"),
        "bid_price": tick.get("bidPrice"),
        "ask_vol": tick.get("askVol"),
        "bid_vol": tick.get("bidVol"),
    }


async def get_quotes(
    symbols: list[str],
) -> dict[str, Optional[Mapping[str, Any]]]:
    """批量取多标的 tick 快照（get_full_tick 原生支持 list）。

    Why 批量：颈线法 ``stop_loss_monitor`` 盘中每 5min 巡查 N 只持仓现价，
    原 ``get_quote`` 单只循环 → N 次 ``get_full_tick`` 线程池调用；
    改批量后 ``get_full_tick(list)`` 一次性返所有标的快照（原生 list 入参，
    xtdata.html 契约），线程池调用 N→1（减少 GIL 切换与 C++ 调用开销）。

    Args:
        symbols: 标的代码列表（如 ``["600000.SH", "000001.SZ"]``）。

    Returns:
        ``{symbol: tick_dict 或 None}``：
        - 正常：``symbol -> {last_price, high_limit, low_limit, open, pre_close, ...}``
        - 缺失（``get_full_tick`` 返 dict 不含该 symbol / 异常 / xtdata 不可用）：
          ``symbol -> None``（调用方按 None 降级，如 stop_loss_monitor 跳过该标的止损检查）
        - 空 list 入参：返 ``{}``（无持仓即无行情查询）

    边界（Grill Me）：
    - xtdata 不可用（CI/开发环境无 xtquant）→ 所有标的值 None，不抛 ImportError
      （risk_shield 据此跳过涨跌停关、stop_loss_monitor 据此跳过现价检查）；
    - ``get_full_tick`` 抛异常（C++ 内部错误）→ 所有标的值 None，不阻断主路径；
    - 返回 dict 必须含全部入参 symbol（缺失的显式 None）——防止下游 ``quotes[sym]``
      抛 KeyError 阻断整个止损监控循环（致命）。
    """
    # 空 list 短路：无持仓即不查行情（避免 xtdata 收到空 list 报错）
    if not symbols:
        return {}
    # xtdata 不可用：全 None 降级（不抛）
    if not _XTDATA_AVAILABLE:
        logger.debug("xtdata 不可用，get_quotes 全返 None（降级模式）")
        return {s: None for s in symbols}
    loop = asyncio.get_running_loop()
    try:
        # 两路同步 C++ 调用分别投同一线程池（run_in_executor 串行 await，不阻塞事件循环）：
        # ① get_full_tick 批量取 tick（原生 list 入参，N→1 次）；
        # ② _fetch_limit_prices_sync 取涨跌停（按日缓存，命中后 0 调用）。
        raw = await loop.run_in_executor(
            None, lambda: xtdata.get_full_tick(symbols)  # type: ignore[union-attr]
        )
        limits = await loop.run_in_executor(
            None, lambda: _fetch_limit_prices_sync(symbols)
        )
    except Exception as exc:
        # 行情查询失败不阻断主路径：捕获记 warning，全 None 让调用方降级
        logger.warning("xtdata.get_full_tick/limit 批量异常 symbols=%s: %s", symbols, exc)
        return {s: None for s in symbols}
    if not raw:
        # xtdata 返空 dict（无任何标的）→ 全 None
        return {s: None for s in symbols}
    # 归一化：驼峰 tick → 下划线 + 注入涨跌停；raw 不含的标的（停牌/退市）归一化后 None。
    # 绝不漏键（下游 quotes[sym] 抛 KeyError 会阻断整个止损/风控循环——致命）。
    return {
        s: _normalize_tick_sync(
            raw.get(s) if isinstance(raw, Mapping) else None,
            *(limits.get(s) or (None, None)),
        )
        for s in symbols
    }


async def get_quote(symbol: str) -> Optional[Mapping[str, Any]]:
    """单标的快照（便利方法，内部委托 ``get_quotes([symbol])[symbol]``）。

    Why 委托：保留单只签名供 risk_shield 第9关涨跌停 / get_positions 市值富化等
    单只消费者无改动复用；实现复用批量逻辑，消除两份并行实现（DRY）。

    Returns:
        tick dict（``{last_price, high_limit, ...}``）或 None。
        返 None 场景同 ``get_quotes``（xtdata 不可用 / 异常 / 不含该标的）。
    """
    return (await get_quotes([symbol])).get(symbol)
