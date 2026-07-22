# -*- coding: utf-8 -*-
"""trading.io.orders — 下单 I/O 包装（副作用壳 · 只搬运不判定）。

物理定位（Layer2 阶段5 · spec §3.5 五层第④层 io/）：
    ``submit_order`` 包装 ``server.services.trading_service.submit_order``（过风控 +
    网关真单），把下单 I/O 收口到 io/ 层。

    **dry_run 作为参数注入**（不读 env）：模式开关（_mode）读取是 orchestrate 职责，
    io/ 只忠实执行调用方给的 dry_run 标志。这样 io/ 可被回测/测试以 dry_run=True
    复用而不耦合进程级 env，且 orchestrate 的「整批影子」语义单点可控。

契约透传（trading_service.submit_order）：
    - dry_run=True  → 返 {"order_id":"", "state":"DRY_RUN", "message":<reason>}（不真下单）；
    - 真单成功      → 返 {"order_id":<seq>, "state":<OrderState.name 字符串>, "message":...}；
    - 挡板命中（非 dry_run）→ **raise RuntimeError**（调用方 try-except 兜底）。

Why 不在本层做挡板/风控判定：
    check_order 10 关风控已在 trading_service.submit_order 内部调 trading.compute.risk
    （functional core），io/ 只是搬运到 trading_service 这条路径，不重复判定也不下推。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def submit_order(order: Any, *, dry_run: bool, confirm: bool = True) -> dict:
    """下单 I/O 包装（透传 trading_service.submit_order）。

    参数：
        order:    OrderRequest（trading.compute.types，由调用方构造）。
        dry_run:  影子模式标志（True=不真下单，返 DRY_RUN）。
        confirm:  二次确认开关（默认 True，引擎自动批量通道见 engine._submit 注释）。

    返回：
        trading_service.submit_order 原样透传（{order_id, state, message}）。

    异常：
        非 dry_run 挡板命中 → raise RuntimeError（透传，由调用方 try-except）。
    """
    from server.services.trading_service import submit_order as svc_submit
    return await svc_submit(order, dry_run=dry_run, confirm=confirm)
