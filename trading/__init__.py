"""真实交易模块：QMT 对接、订单状态机、风控挡板。

Layer2 阶段5 五层定型（spec §3.5）内部结构：
    trading/
    ├─ types/        ① 纯数据契约（OrderState 枚举等 · 零外部依赖单源）
    ├─ compute/      ② 纯决策函数（止损/风控/离场/对账 · functional core · 阶段2 done）
    ├─ state/        ③ reducer 式状态机（薄壳 · 颈线法靠 broker 跟踪状态 · 暂无实质内容）
    ├─ io/           ④ 副作用壳（下单/撤单/查持仓/查行情/熔断撤单 · 只调broker+data+types）
    └─ orchestrate/  ⑤ 编排（eod_plan/pre_open/stop_loss/post_close 四触发点 · 只连线不判定）

    顶层 engine.py 留根（保 tests/trading/test_engine.py 等 monkeypatch
    engine.calendar/circuit_breaker/qmt_market_data/reconcile_job 模块级属性兼容），
    orchestrate/ 作门面 re-export 四触发点 + TradingEngine。

职责（历史描述 · 仍成立）：
1. 订单状态机（处理断线、限频、部分成交）
2. QMT 实盘执行网关（xtquant 异步封装）
3. 风控挡板（纯函数，下单前 10 关校验）
4. 保证金敞口监控

注：Mock 交易模拟层（MockBroker）Layer2 阶段4 已迁 backtest/mock_broker.py（回测撮合
专属，与交易层分离——回测求变、交易求稳）。消费者改 ``from backtest import MockBroker``。
ExecutionExecutor Protocol（依赖反转抽象）迁 trading/protocols.py（spec §5）。
"""

from .order_state import OrderStateMachine
from .types import OrderState


# ============================================================================
# QmtExecutionGateway 延迟加载（Layer2 阶段3 必须改为 lazy）
# ============================================================================
# Why lazy：broker.base 在模块加载期 import trading.compute.types / trading.order_state
# （broker 叶子的合法单向依赖），这会触发 trading/__init__.py 加载。若此时 __init__
# eager ``from broker.qmt import QmtExecutionGateway``，会拉起 broker.qmt → broker.base
# （partially-initialized）→ ImportError 循环。改 lazy 后，trading/__init__ 加载完成
# 不触 broker.qmt；后续真正取 QmtExecutionGateway 时 broker.base 早已加载完毕。
#
# 兼容：``from trading import QmtExecutionGateway``（tests/test_risk_shield.py 等用）
# 经模块级 __getattr__ 懒加载，零改动可用。
def __getattr__(name: str):
    if name == "QmtExecutionGateway":
        # 真身已迁 broker.qmt（Layer2 阶段3 剥 broker 叶子包）。
        # 顶部 try/except 容错 import xtquant：无 xtquant 的开发/CI 环境仍可加载。
        from broker.qmt import QmtExecutionGateway as _G
        globals()["QmtExecutionGateway"] = _G  # 缓存，后续直取
        return _G
    raise AttributeError(f"module 'trading' has no attribute {name!r}")


__all__ = [
    "OrderStateMachine",
    "OrderState",
    "QmtExecutionGateway",
]
