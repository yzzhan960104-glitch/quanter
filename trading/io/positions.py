# -*- coding: utf-8 -*-
"""trading.io.positions — 查持仓 I/O 包装（副作用壳 · 只搬运不判定）。

物理定位（Layer2 阶段5 · spec §3.5 五层第④层 io/）：
    ``fetch_positions`` 包装 ``gw._fetch_broker_positions()``，把网关持仓查询的
    async I/O 收口到 io/ 层。orchestrate（stop_loss_monitor 编排）调本函数拿持仓
    后交给 compute 判定（跌破止损），实现「查仓」与「判跌破」的物理分离。

Why 只搬运不判定：
    持仓查询本身无业务语义（只是拉券商状态快照）；「哪些持仓该止损」是 compute 职责。
    把两者混在一起（旧 stop_loss_monitor 四缠热点）会导致回测路径无法复用判定逻辑
    （回测无 gw，但判定是该不该卖——纯函数，应可独立测试）。
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)


async def fetch_positions(gw: Any) -> Mapping[str, Any]:
    """查询网关真实持仓（包装 gw._fetch_broker_positions）。

    参数：
        gw: 执行网关实例，需暴露 async ``_fetch_broker_positions()``（契约：
            返 {symbol: {volume, avg_price, ...}} dict-of-dict，T7 扩展后含成本价/昨夜股）。

    返回：
        {symbol: position_dict} 持仓快照。

    Why 包装而非直调：
    - 统一 io/ 入口（orchestrate 只 import trading.io，不直接触 gw 私有方法名）；
    - 便于测试 monkeypatch（patch trading.io.positions.fetch_positions 单点）；
    - 未来若持仓源换（如加本地缓存/多网关聚合），只改 io/ 一处。
    """
    return await gw._fetch_broker_positions()
