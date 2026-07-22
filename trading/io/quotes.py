# -*- coding: utf-8 -*-
"""trading.io.quotes — 查行情 I/O 包装（副作用壳 · 只搬运不判定）。

物理定位（Layer2 阶段5 · spec §3.5 五层第④层 io/）：
    ``fetch_quotes`` 包装 ``broker.qmt_quote.get_quotes``（批量取 last_price 快照），
    把行情查询的 async I/O 收口到 io/ 层。orchestrate（stop_loss_monitor）调本函数
    拿现价后交给 compute 判定（跌破止损），实现「查价」与「判跌破」分离。

⚠️ live 行情源依赖（物理边界 · live 前必修 follow-up）：
    底层 ``broker.qmt_quote.get_quotes`` 走 xtdata.get_full_tick，仅在 miniQMT 通道
    可用时返回有效快照；EMT 网关无 xtdata 行情源，止损链路 live 前必须另接行情源。
    本包装不解决行情源缺失——None/NaN 现价由 orchestrate 传给 compute 判定时跳过
    （无价不能判跌破，盲单 = 卖错价 = 致命）。
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)


async def fetch_quotes(symbols: list[str]) -> Mapping[str, Any]:
    """批量取多标的现价快照（包装 broker.qmt_quote.get_quotes）。

    参数：
        symbols: 标的代码列表（形如 ["300001.SZ", ...]）。

    返回：
        {symbol: quote_dict} 快照；quote_dict 含 last_price 等字段。
        某标的 quote=None 或 last_price=None/NaN 表示该标的无有效现价
        （由调用方/orchestrate 在传给 compute 判定时跳过，不发盲单）。

    Why 批量而非循环单只：
        N 只持仓原 N 次 get_full_tick 线程池调用 → 1 次（xtdata 原生 list 入参），
        减少 GIL 切换与 C++ 调用开销；颈线法盘中 5min 巡查场景下显著降低行情查询延迟。
    """
    # 延迟 import broker.qmt_quote（避免 io 顶层拉起 broker 依赖链；broker 是叶子包
    # 合法依赖，但延迟 import 保持 io/ 模块加载轻量，且与 engine 既有 import 模式一致）。
    from broker.qmt_quote import get_quotes
    return await get_quotes(symbols)
