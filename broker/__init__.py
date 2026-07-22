# -*- coding: utf-8 -*-
"""broker/ —— 实盘执行叶子包（Layer2 阶段 3 · design §3.3）。

物理定位：本包是剥自 trading/ 的【干净执行叶子】，承载「下单 + 撤单 + 查持仓 +
查资金 + 实时行情 + 行情清洗 + Mock 网关」全执行域。零反向依赖 trading 编排
（trading.engine / orchestrate / signal_runner / risk_shield / stop_loss /
circuit_breaker / dynamic_whitelist 等一律不 import）。

允许的依赖方向（单向，无循环）：
- broker → trading.compute.*（functional core：reconcile 纯函数 / OrderRequest dataclass）
- broker → trading.order_state（OrderState 状态机枚举）
- broker → trading.types（如需）
- broker → xtquant（vendor，sys.path 注入，延迟容错 import）
- broker → data/*（行情清洗若需）
- broker → core.notifier（infra 别名垫片，断线告警 fire_and_forget）

模块清单（design §3.3）：
- ``broker.base``：BaseExecutionGateway 抽象基类 + OrderResult（含 spec §3.3 新增
  query_asset/get_quote 抽象，补齐原基类缺口）；
- ``broker.qmt``：QmtExecutionGateway（miniQMT/xtquant 异步实现，git mv 自
  trading/qmt_gateway.py）；
- ``broker.qmt_quote``：xtdata 行情封装（驼峰归一化 + 涨跌停注入 + 批量取数，
  git mv 自 trading/qmt_market_data.py）；
- ``broker.mock``：MockExecutionGateway（内存 Mock，spec §3.3 新增抽象的占位实现）。

兼容垫片（strangler 铁律①）：既有 20+ 处 ``from trading.execution_gateway import X``
/ ``from trading.qmt_gateway import Y`` / ``from trading import qmt_market_data``
调用零改动——trading 侧三处垫片 re-export 本包符号（见 trading/execution_gateway.py、
trading/qmt_gateway.py、trading/qmt_market_data.py）。
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
# QMT 实盘实现（延迟 import 容错：无 xtquant 的开发/CI 环境仍可加载本包）
from broker.qmt import (  # noqa: F401
    QmtExecutionGateway,
)
# 行情模块级函数（单只/批量快照）
from broker.qmt_quote import (  # noqa: F401
    get_quote,
    get_quotes,
)

__all__ = [
    "BaseExecutionGateway",
    "OrderResult",
    "MockExecutionGateway",
    "QmtExecutionGateway",
    "get_quote",
    "get_quotes",
]
