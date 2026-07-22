# -*- coding: utf-8 -*-
"""trading.state — reducer 式状态机层（薄壳 · 颈线法实盘路径暂无自定义 reducer）。

物理定位（Layer2 阶段5 · spec §3.5 五层定型第③层）：
    本层设计意图是放置【reducer 式纯状态机】——给定 (state, event) → new_state 的
    纯函数式状态迁移（无副作用、确定性）。但经颈线法实盘路径审计：

颈线法实盘状态跟踪现状（为何本层暂无实质内容）：
    颈线法实盘路径（pre_open 挂单 → 成交 → stop_loss 监控 → 平仓）的订单/持仓状态
    由【broker 网关】跟踪，不由交易层自维护 reducer：
    - 订单状态：QmtExecutionGateway._orders dict 存 OrderState 枚举，由 on_stock_trade/
      on_order_error/on_cancel_error 回调推进（broker 跟踪）；
    - 持仓状态：gw._fetch_broker_positions() 实时拉券商持仓快照（broker 跟踪）；
    - 止损触发：stop_loss_monitor 每 5min 用 broker 持仓 + 现价判定，无中间状态机。

    故颈线法路径【不需要】ARMED→FILLED→CLOSED 这类交易层自维护 reducer——强建空壳
    反而引入「broker 状态 vs 交易层状态」双源失配风险（broker 已成交、reducer 还在
    ARMED，谁是真理源？答：broker 是真理源，reducer 是冗余）。

既有 OrderStateMachine 的定位（不迁入本层）：
    trading/order_state.py::OrderStateMachine 是【有状态可变对象】（self.current_state +
    callbacks + _transition_to 副作用），属 imperative shell 而非 reducer（reducer 应
    是纯函数 (state, event) → new_state）。它被 broker/base.py / backtest/mock_broker.py
    消费（执行域），不属 functional core 的 state/ 层。保留在 order_state.py（imperative
    shell 域），本 state/ 层不收纳它。

stage5 follow-up（若未来需要再建）：
    若颈线法扩展条件单（如 OCO 二选一止损止盈、条件触发挂单）需交易层自维护状态机，
    则在本层建 reducer（纯函数 (state, event) → new_state + 不可变 state 值对象）。
    当前 YAGNI——不建空壳逻辑。
"""
from __future__ import annotations

# 本层当前无公开 API（薄壳·文档型）。未来若建 reducer，在此 re-export。
__all__: list[str] = []
