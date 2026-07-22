"""
trading/qmt_market_data.py
==========================
【Layer2 阶段 3 · strangler 铁律① · 兼容垫片】

物理真身（``get_quote`` / ``get_quotes`` / ``_fetch_limit_prices_sync`` /
``_normalize_tick_sync`` + 模块级可变全局 ``xtdata`` / ``_XTDATA_AVAILABLE`` /
``_LIMIT_PRICE_CACHE``）已 git mv 迁至 ``broker/qmt_quote.py``。

本模块【re-export 转发】公开 API（``get_quote`` / ``get_quotes``），保既有
``from trading import qmt_market_data`` + ``qmt_market_data.get_quote(sym)`` 等
非内部消费点零改动可用（含 trading/engine.py、trading_service.py、scripts/qmt_live_smoke*）。

⚠️ monkeypatch 内部全局（``xtdata`` / ``_XTDATA_AVAILABLE`` / ``_LIMIT_PRICE_CACHE``）
的消费点（单测 tests/test_qmt_market_data.py + tests/trading/test_qmt_market_data.py）
**已改指 broker.qmt_quote**：这些测试 patch 的是真身模块的全局，垫片的 re-export
副本与真身不是同一对象，patch 垫片副本不会影响真身 ``get_quotes`` 读的全局。
故此类「内部全局 patch」消费点必须直接指 broker.qmt_quote（非本垫片）。

设计哲学（CLAUDE.md strangler 模式）：剥真身到 broker 后，旧路径作纯垫片兜底，
非内部消费点零改动；内部耦合消费点（单测）显式改指真身模块。
"""
from __future__ import annotations

# 真身 re-export（broker/qmt_quote.py —— broker 叶子包，零反向依赖 trading 编排）
from broker.qmt_quote import (  # noqa: F401
    get_quote,
    get_quotes,
    _normalize_tick_sync,
    _fetch_limit_prices_sync,
)
# 模块级全局也 re-export（保 ``qmt_market_data.xtdata`` 等只读访问可用；但
# monkeypatch 须改指 broker.qmt_quote，见模块 docstring）
from broker.qmt_quote import (  # noqa: F401
    xtdata,
    _XTDATA_AVAILABLE,
    _LIMIT_PRICE_CACHE,
)
