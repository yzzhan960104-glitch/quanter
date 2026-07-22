# -*- coding: utf-8 -*-
"""trading.io — 副作用壳（imperative shell · 只搬运不判定）。

物理定位（Layer2 阶段5 · spec §3.5 五层定型第④层）：
    本子包收口所有【交易 I/O 副作用】：下单/撤单/查持仓/查行情/熔断撤单动作。
    设计契约（硬约束 · 由审查 grep 守护）：
    - **只调 broker + data + types**：不反向 import compute/orchestrate（避免循环）；
    - **零业务判定**：if 分支只允许错误处理（try/except）或 None 防御，止损/风控/
      离场等业务 if 判定一律下推 compute（functional core 职责）；
    - **只搬运**：把网关/行情的 I/O 结果原样返回给 orchestrate，由后者调 compute 判定。

为什么不把判定也放这（看似更聚合）：
    回测与实盘共用同一份决策逻辑的前提是 compute 零 I/O 依赖（test_compute_purity）。
    若把止损/风控判定写进 io，则回测路径要么复制一份判定（口径分叉经典翻车），
    要么反向依赖 io（破坏 compute 纯净）。故 io 严格只搬运，判定单源在 compute。

模块清单：
- orders.py   下单（submit_order）/撤单（cancel_order）的 I/O 包装（调 trading_service /
              broker 网关；dry_run 作为【参数】注入，不读 env——env 读取是 orchestrate 职责）；
- positions.py 查持仓（fetch_positions，调 gw._fetch_broker_positions）；
- quotes.py   查行情（fetch_quotes，调 broker.qmt_quote.get_quotes 批量取 last_price）；
- breaker.py  熔断撤单动作（cancel_all_open_orders，调 gw.cancel_order；判定在 compute.breaker）。

迁移路径（strangler 铁律①）：
    cancel_all_open_orders 原物理定义在 trading/circuit_breaker.py（与 check_daily_loss_limit
    纯判定 re-export 混居），本阶段把副作用部分迁 io/breaker.py，circuit_breaker.py
    改垫片 re-export（保 ``from trading.circuit_breaker import cancel_all_open_orders,
    check_daily_loss_limit`` 调用零改动）。
"""
from __future__ import annotations

# io/ 公开 API（orchestrate 消费面）：
from trading.io.breaker import cancel_all_open_orders
from trading.io.positions import fetch_positions
from trading.io.quotes import fetch_quotes
from trading.io.orders import submit_order

__all__ = [
    "cancel_all_open_orders",
    "fetch_positions",
    "fetch_quotes",
    "submit_order",
]
