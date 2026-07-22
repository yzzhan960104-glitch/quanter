# -*- coding: utf-8 -*-
"""trading/circuit_breaker.py — 垫片 re-export（Layer2 阶段5 · strangler 铁律①）。

Layer2 阶段5 职责切分（functional core / imperative shell 五层定型）：
- 【纯判定】``check_daily_loss_limit``（日亏判定）在 trading/compute/breaker.py
  （functional core），本模块经垫片 re-export 转发——既有
  ``from trading.circuit_breaker import check_daily_loss_limit`` 调用零改动。
- 【I/O 副作用】``cancel_all_open_orders``（撤销网关未终态单——await cancel_order
  调券商 I/O）已迁 trading/io/breaker.py（副作用壳层），本模块经垫片 re-export 转发——
  既有 ``from trading.circuit_breaker import cancel_all_open_orders`` 调用零改动。

Why 拆两处（compute.breaker + io.breaker）而非留本模块：
    五层定型后，纯判定（compute/）与 I/O 副作用（io/）物理分离——回测路径只引 compute，
    实盘 orchestrate 引 compute（判定是否熔断）+ io（执行撤单）。本垫片模块仅为兼容
    既有调用入口，新代码应直指 compute.breaker / io.breaker。

物理意图（Why 风控阈值定 -3%）：
- 日内 3% 权益回撤在 A 股单一策略层已属显著异常（多数交易日波动远小于此），
  一旦触及即视作「策略与环境失配」的强信号，宁可当日停手、次日复盘重启，
  也不容忍异常持续累积成穿仓。
"""
from __future__ import annotations

# 纯判定函数 re-export（strangler 铁律① · 垫片）——物理定义在 trading/compute/breaker.py。
from trading.compute.breaker import check_daily_loss_limit  # noqa: F401
# 副作用动作 re-export（strangler 铁律① · 垫片）——物理定义迁 trading/io/breaker.py。
from trading.io.breaker import cancel_all_open_orders  # noqa: F401

__all__ = ["check_daily_loss_limit", "cancel_all_open_orders"]
