# -*- coding: utf-8 -*-
"""research/committee —— 策略评审委员会（2026-09-06 八小时会战主线）。

物理意图：移植 TradingAgents 的辩论协议（分析师→多空攻防→风控三辩→基金经理
裁决），但评审对象是**策略本身**而非单只股票；LLM 通路走本项目已验证的
GlmClient anthropic 端点（tools 协议 09-06 探测 PASS，见 diag/glm_tools_probe.py），
数据 100% 来自本地湖/回测语料/7002 实盘——agent 通过查询工具自证，防编造。

红线（与 08-25 全停裁决、09-04 幸存者偏差红线对齐）：
  - 委员会输出=假设与观点，永不直接当信号/参数修改指令；
  - 可参数化提案必须走 explore_loop→evaluate_replay 全窗口验证→提案流人审；
  - 事实纪律：所有数字必须可溯源到工具输出或简报，引擎记录完整 transcript。
"""
