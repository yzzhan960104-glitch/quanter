# -*- coding: utf-8 -*-
"""【转发垫片】caisen/backtest_replay.py —— 物理实体已迁至 execution/backtest_replay.py（Step4c 批 B）。

存在原因：facade（Task2.1）+ replay_worker + tests + scripts + __main__ 大量使用
``from caisen.backtest_replay import replay, ReplayReport, ReplayAborted`` 或
``from caisen import backtest_replay``，Python 必须能 import 到真实的
``caisen.backtest_replay`` 模块对象。

采用 sys.modules 别名：使 ``caisen.backtest_replay`` 与 ``execution.backtest_replay`` 成为
【同一模块对象】，保证 monkeypatch 等基于模块身份的操作在两条路径下完全等价
（strangler 铁律①；Task3.2 沉淀）。

迁移链与垫片层级（Step4c 沉淀 · 防 execution→caisen 反向 import 循环）：
    本顶层垫片【直指真身】``execution.backtest_replay``（单层别名），不经过
    caisen.infra.backtest_replay 中间垫片。原因：execution/replay_worker.py 迁后仍
    ``from caisen.backtest_replay import replay``，若顶层垫片经 ``from caisen.infra
    import backtest_replay`` 二跳，会在 execution/__init__ 触发 caisen 反向 import 时
    与 caisen.infra 包初始化形成循环竞态（顶层垫片可能捕获到未替换的 infra 垫片壳子）。
    直指 execution.backtest_replay 单层别名消除该竞态（与 Step3 ``caisen.plan →
    caisen.engines.plan`` 单层同模式）。``caisen.infra.backtest_replay`` 垫片独立兜底
    ``from caisen.infra.backtest_replay import X`` 用法（亦直指 execution.backtest_replay）。

新代码请直接使用 ``from execution.backtest_replay import ...``。
"""
from execution import backtest_replay as _real  # noqa: F401
import sys as _sys
_sys.modules[__name__] = _real
