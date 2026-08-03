# -*- coding: utf-8 -*-
"""research 策略研究观测层（2026-08-03 新增 · Agent 观察环地基）。

物理定位：长周期 Agent 自主优化的"环 1 观察"——每天盘后把实盘成交（fill 去重
归因）、回测期望（replay_tasks.db 最近 SUCCESS）、数据/实验状态组装成结构化
研究摘要（research_digest），并做粗粒度漂移对比。纯函数可单测，loader 可注入；
不触达实盘下单（只读观测），不依赖 trading.engine（分层铁律）。

模块：
    digest：摘要纯函数 + CSV/task 库 loader + main 组装入口。
"""
