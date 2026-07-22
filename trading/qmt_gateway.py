"""
trading/qmt_gateway.py
======================
【Layer2 阶段 3 · strangler 铁律① · 兼容垫片】

物理真身 ``QmtExecutionGateway``（+ 状态映射辅助 ``_map_qmt_status`` /
``_assert_status_contract`` + xtquant 类 ``XtQuantTrader`` /
``XtQuantTraderCallback`` / ``StockAccount`` / ``xtconstant`` + ``_QMT_*`` 状态
字面量）已 git mv 迁至 ``broker/qmt.py``。

本模块【仅 re-export 转发】，保既有全仓多处
``from trading.qmt_gateway import QmtExecutionGateway`` / 单测
``qmt_gateway.XtQuantTrader``（conftest 注入的 FakeXtQuantTrader）/ ``_map_qmt_status``
/ ``_assert_status_contract`` 等调用零改动可用。

设计哲学（CLAUDE.md strangler 模式）：剥真身到 broker 后，旧路径作纯垫片兜底。
"""
from __future__ import annotations

# 真身 re-export（broker/qmt.py —— broker 叶子包，零反向依赖 trading 编排）。
# 含 xtquant 类（XtQuantTrader/XtQuantTraderCallback/StockAccount/xtconstant），
# 单测经 ``qmt_gateway.XtQuantTrader`` 读 conftest 注入的 FakeXtQuantTrader 类对象。
from broker.qmt import (  # noqa: F401
    QmtExecutionGateway,
    _map_qmt_status,
    _assert_status_contract,
    XtQuantTrader,
    XtQuantTraderCallback,
    StockAccount,
    xtconstant,
)
