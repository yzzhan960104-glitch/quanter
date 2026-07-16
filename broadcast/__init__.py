# -*- coding: utf-8 -*-
"""每日行情播报包（feat/daily-market-brief）。

扁平管道（无框架、零新依赖）：
- brief.py    取数(DataLakeReader) + pandas 聚合 + 模板渲染 → Markdown（纯函数·可单测）
- push.py     subprocess 调 dws send-by-bot 出站（零自写加签）
- __main__.py CLI(--date/--dry-run/--force) + logs/.last_broadcast 幂等去重

设计见 docs/superpowers/specs/2026-07-16-daily-market-brief-design.md。
"""
