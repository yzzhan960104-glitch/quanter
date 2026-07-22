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
        # xtdata.get_full_tick 是同步 C++ 调用，经线程池投递绝不阻塞事件循环。
        # 原生支持 list 入参（xtdata.html 契约）——批量 1 次调用而非 N 次。
        raw = await loop.run_in_executor(
            None, lambda: xtdata.get_full_tick(symbols)  # type: ignore[union-attr]
        )
    except Exception as exc:
        # 行情查询失败不阻断主路径：捕获记 warning，全 None 让调用方降级
        logger.warning("xtdata.get_full_tick 批量异常 symbols=%s: %s", symbols, exc)
        return {s: None for s in symbols}
    if not raw:
        # xtdata 返空 dict（无任何标的）→ 全 None
        return {s: None for s in symbols}
    # raw 可能只含部分 symbol（停牌/退市/代码错误不在返回 dict 里）；
    # 缺失的标 None（绝不漏键，否则下游 quotes[sym] 抛 KeyError）
    return {
        s: (raw.get(s) if isinstance(raw, Mapping) else None) for s in symbols
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
