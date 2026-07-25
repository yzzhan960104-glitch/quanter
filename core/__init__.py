"""core 包：已解散（2026-07 架构治理）。

历史：core/ 曾是跨领域杂物间，Step1 起按 strangler 模式逐步迁出：
  - indicator（atr）→ factors/atr.py（转发垫片 core/indicator.py 已删）
  - notifier      → infra/notifier.py（转发垫片 core/notifier.py 已删）
  - macro_regime / 宏观 CTA → 整体下线删除（CreditRegime 已删；
    数据管道 macro_credit 湖 + data/tools/sync_macro_credit.py 保留待重建）

本包现为空壳，仅保留目录占位（避免破坏潜在 ``import core`` 的历史引用）。
新代码勿再向 core/ 添加任何模块——通知走 infra/notifier，因子走 factors/，
宏观逻辑后续重建时另立独立 macro/ 包。
"""
