# -*- coding: utf-8 -*-
"""钉钉机器人总管包。

统管 8 个钉钉机器人，按生命周期分两类、走两条独立管线（无框架、零新依赖）：

【push 类】（trading / data / strategy）——一次性播报，到点或手工触发即推即退
- brief_*.py    各自取数 + pandas 聚合 + 模板渲染 → Markdown（纯函数·可单测）
- brief.py      共享工具：BriefResult 数据类 / _clean_markdown / _weekday_zh（被三个 brief_*.py 复用）
- push.py       subprocess 调 dws send-by-bot 出站（零自写加签）
- 触发：schtasks 到点拉起 `python -m broadcast` 或手工 `--force`，推完即退出

【connect 类】（cli / trading_q / data_q / strategy_q / review）——常驻交互式问答
- connect_manager.py    后台托管 `dws dev connect <bot>`：Popen 拉起 / PID 登记 /
                        日志重定向 / taskkill /T 树杀 / 僵尸进程清理 / 健康重启

【CLI 入口】
- __main__.py    子命令路由：`push`（默认，向后兼容 schtasks 单调用）/ `connect`（托管常驻）
                 无子命令时=push，保留旧 schtasks 注册项零迁移

设计见 docs/superpowers/specs/2026-07-26-broadcast-robot-manager-design.md。
"""
