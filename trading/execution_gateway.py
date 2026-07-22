"""
trading/execution_gateway.py
============================
【Layer2 阶段 3 · strangler 铁律① · 兼容垫片】

物理真身已迁 broker 叶子包：
- ``BaseExecutionGateway`` / ``OrderResult`` → ``broker/base.py``
- ``MockExecutionGateway`` → ``broker/mock.py``
- ``QmtExecutionGateway`` → ``broker/qmt.py``（原 qmt_gateway.py）

本模块【仅 re-export 转发】，保既有全仓 20+ 处
``from trading.execution_gateway import reconcile, OrderRequest, BaseExecutionGateway,
MockExecutionGateway, OrderResult, ...`` 调用零改动可用。

re-export 三源：
1. broker 叶子（base/mock）：执行抽象 + Mock 实现；
2. trading.compute.reconcile（阶段2 functional core）：reconcile / PositionDrift /
   ReconciliationResult —— 【风控对账语义留 trading/compute/，不进 broker】；
3. trading.compute.types（阶段2 functional core）：OrderRequest dataclass。

设计哲学（CLAUDE.md strangler 模式）：剥真身到 broker 后，旧路径作纯垫片兜底，
让消费点按「最小改动」自行决定何时改指 broker.*，而非一刀切炸全仓调用方。
"""
from __future__ import annotations

# ============================================================================
# broker 叶子 re-export（Layer2 阶段3 真身迁移目标）
# ============================================================================
from broker.base import (  # noqa: F401
    BaseExecutionGateway,
    OrderResult,
)
from broker.mock import (  # noqa: F401
    MockExecutionGateway,
)

# ============================================================================
# 纯决策 re-export（Layer2 阶段2 · functional core · 垫片）
# 物理定义在 trading/compute/（reconcile.py / types.py）。下方 re-export 保证既有
# ``from trading.execution_gateway import reconcile, OrderRequest, ...`` 调用零改动。
# reconcile 真身【刻意留 trading/compute/】不进 broker：对账是风控语义，非执行语义。
# ============================================================================
from trading.compute.reconcile import (  # noqa: F401
    reconcile,
    PositionDrift,
    ReconciliationResult,
)
from trading.compute.types import (  # noqa: F401
    OrderRequest,
)


# ============================================================================
# 注：宏观一票否决网关（VetoedError / MacroAwareGateway）已于 B-7 修复时移除。
# 原实现（Task 14 宏观 CTA Epic 3）存在三重缺陷：① 不继承 BaseExecutionGateway；
# ② submit_order 同步签名 (order, regime) 与基类 async(order) 不兼容；③ 就地改写
# frozen OrderRequest.quantity 会抛 FrozenInstanceError。蔡森专精化（Phase 1）已删除
# 宏观 CTA 策略，当前唯一策略 caisen 为纯价量形态学、不消费 CreditRegime，该死代码
# 零生产接入（仅文档/单测引用）。按 YGNI 删除；若重启宏观风控，须重新设计为正确的
# BaseExecutionGateway 子类（async submit_order、返回新 OrderRequest 而非就地改 frozen）。
# ============================================================================
