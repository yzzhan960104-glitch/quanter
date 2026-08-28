# -*- coding: utf-8 -*-
"""broker/ —— 执行网关契约叶子包（QMT 退役后仅存契约与 Mock）。

W7（2026-08-28 完成退役）：qmt/qmt_io/qmt_connection/qmt_quote 等实盘网关实现
随 QMT 退役 P3 删除（掘金为唯一实盘平台）。本包现仅存：
    - base.py   ExecutionGateway 抽象契约（回测/研究侧的类型语言；考古接口）
    - mock.py   MockBroker（部分回测/测试消费）
历史完整实现见 archive/qmt-stack-final 分支。
"""
from __future__ import annotations

# 基类 + 订单结果（broker 叶子的契约根）
from broker.base import (  # noqa: F401
    BaseExecutionGateway,
    OrderResult,
)
# Mock 参考实现
from broker.mock import (  # noqa: F401
    MockExecutionGateway,
)
# QMT 实盘实现已退役删除（2026-08-27 · QMT 退役 P3，掘金为唯一实盘平台；
# archive/qmt-stack-final 分支可考）。base/mock 保留——backtest mock 与执行网关
# 契约测试仍消费。

__all__ = [
    "BaseExecutionGateway",
    "OrderResult",
    "MockExecutionGateway",
]
