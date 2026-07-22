# -*- coding: utf-8 -*-
"""trading.orchestrate — 编排层（连线 compute 判定 + io 副作用 · 自身无业务判定）。

物理定位（Layer2 阶段5 · spec §3.5 五层定型第⑤层）：
    本子包是二期自动交易引擎的【编排层】——组合 compute（纯判定）+ io（副作用），
    按 APScheduler 四触发点节奏连线。设计契约（硬约束）：
    - **只连线不判定**：业务 if 判定（止损/风控/离场）= compute 职责，本层只调 compute
      拿结果后决定走哪条编排支路（如下单 vs 跳过）；if 分支只允许错误处理或编排调度
      （如「未确认 → 跳过挂单」「非盘中 → 跳过止损」这种节奏性跳过，非业务判定）。
    - **连线四触发点**：eod_plan / pre_open / stop_loss_monitor / post_close。

四触发点物理节奏（术语对齐 T 日盘后扫盘 → T+1 执行）：
    eod_plan   15:35 T 日盘后：扫颈线法信号 → build_orders → save_plan（confirmed=False）
                → push 钉钉（待研究员确认）。本阶段绝不下单（机器只产计划，人审是闸）。
    pre_open   09:22 T 日开盘前：① 撤昨日遗留未成交单 ② 读已确认计划 → 注入动态白名单
                → 挂限价买 + 止盈限价卖（逐单 try-except 兜底）。
    stop_loss  每 5min 盘中：io 查持仓 + io 查现价 → compute.should_trigger_stop 判定
                → io 下卖出单（qty 来自 io 持仓，绝不硬编码——live 卖错数量 = 致命）。
    post_close 15:30 盘后：对账（run_reconcile）+ 清动态白名单。熔断连线留 follow-up
                （需 equity 数据源，本阶段显式 DEFER）。

为什么 engine.py 留 trading/ 根而非全量迁 orchestrate/（务实决策）：
    既有测试（tests/trading/test_engine.py / test_engine_eod_injection.py /
    tests/experiment/test_e2e_eod_to_plan.py）大量 monkeypatch ``engine.calendar`` /
    ``engine.circuit_breaker`` / ``engine.qmt_market_data`` / ``engine.reconcile_job``
    等模块级属性——若 git mv engine.py → orchestrate/engine.py 会破坏 ``from trading
    import engine`` 入口与这些 patch 点。故采用「真身留根 + orchestrate/ re-export
    门面」的务实结构：engine.py 物理留在 trading/（保 patch 点），orchestrate/ 作
    门面包 re-export 四触发点 + TradingEngine，新代码用 ``from trading.orchestrate
    import eod_plan`` 等价入口。

stop_loss_monitor 四缠拆解（spec §3.5 最大债 · 本层核心交付）：
    旧 engine.stop_loss_monitor 一个方法混了四职责（判定+查价查仓+下单+调度），
    本阶段拆为：
    - compute 判定：should_trigger_stop(price, stop_price) 纯函数（compute/stop.py）；
    - io 查价查仓：fetch_positions / fetch_quotes（io/positions.py + io/quotes.py）；
    - io 下单：submit_order（io/orders.py）；
    - orchestrate 调度：stop_loss_monitor 编排（本层 engine.py，只连线 + 错误处理）。
    决策逻辑零改（T1 golden 数值不变），纯结构拆解。
"""
from __future__ import annotations

# 编排层门面：re-export 四触发点 + TradingEngine（真身在 trading/engine.py）。
from trading.engine import (  # noqa: F401
    eod_plan,
    pre_open,
    stop_loss_monitor,
    post_close,
    TradingEngine,
)

__all__ = [
    "eod_plan",
    "pre_open",
    "stop_loss_monitor",
    "post_close",
    "TradingEngine",
]
