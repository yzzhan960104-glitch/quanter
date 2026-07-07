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


async def get_quote(symbol: str) -> Optional[Mapping[str, Any]]:
    """经线程池取 xtdata.get_full_tick([symbol])，返单标的快照 dict。

    返回字段（来源 xtdata get_full_tick 契约）：
        last_price / high_limit / low_limit / open / pre_close ...
    返回 None 的场景：
        - xtdata 不可用（_XTDATA_AVAILABLE=False）
        - get_full_tick 抛异常（C++ 内部错误）
        - 返回空或不含该标的
    调用方（risk_shield / get_positions）必须容忍 None。
    """
    if not _XTDATA_AVAILABLE:
        logger.debug("xtdata 不可用，get_quote 返 None（降级模式）")
        return None
    loop = asyncio.get_running_loop()
    try:
        raw = await loop.run_in_executor(None, lambda: xtdata.get_full_tick([symbol]))  # type: ignore[union-attr]
    except Exception as exc:
        # 行情查询失败不阻断主路径：捕获记 warning，返 None 让调用方降级
        logger.warning("xtdata.get_full_tick 异常 symbol=%s: %s", symbol, exc)
        return None
    if not raw or symbol not in raw:
        return None
    return raw[symbol]
