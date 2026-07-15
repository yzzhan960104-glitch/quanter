"""core 包：跨领域残留（解散进行中 · Step1）。

Step1 解散进度：
  - indicator（atr）→ factors/atr.py ✅（core/indicator.py 留转发垫片）
  - notifier      → infra/notifier.py ✅（core/notifier.py 留转发垫片）
  - macro_regime  → 暂留（最终归模型层·宏观域，随 Step3/4 迁出）
新代码勿再向 core/ 添加无关职责模块。
"""
